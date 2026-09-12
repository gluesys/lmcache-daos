/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Gluesys Co., Ltd. */
/*
 * Throughput of the NIXL DAOS backend, driven directly (no agent) so the
 * numbers describe the backend rather than NIXL's dispatch.
 *
 * Comparable by construction to tests/obj_latency.c in lmcache-daos, which
 * measured the same shapes through libdaos with no NIXL in the path:
 * 120 objects x 40 "layers" x 1 MiB = 4.69 GiB. The question this answers is
 * what the backend costs on top of the raw object API, and whether the folding
 * prepXfer() does actually shows up.
 *
 *   -p pool -c cont
 *   -o N   objects   (default 120)
 *   -l N   layers per object (default 40)
 *   -s N   bytes per layer (default 1048576)
 *   -i N   in-flight requests (default 16)
 *   -r N   timed rounds, best reported (default 3)
 *   -f 0|1 fold: 1 = all layers of an object in one request (default 1)
 *   -g 0|1 staging buffers in GPU memory (default 0)
 *
 * -g needs a plugin built against a client that exports daos_obj_fetch_gpu()
 * and the GDS transport environment (D_MEM_DEVICE=1 and a CUDA-built
 * libfabric ahead of the stock one on LD_LIBRARY_PATH). Without those Mercury
 * refuses the bulk handle with HG_OPNOTSUPPORTED rather than falling back, so
 * a GPU run either measures the GPU path or fails outright -- it cannot
 * quietly measure a host bounce.
 */
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include <unistd.h>

#ifdef BENCH_HAVE_CUDA
#include <cuda_runtime.h>
#endif

#include "daos_backend.h"

namespace {

struct opts {
    std::string pool = "attr1", cont = "nixlbench";
    int objs = 120, layers = 40, inflight = 16, rounds = 3;
    size_t lsize = 1ull << 20;
    int fold = 1;
    int gpu = 0;
};

double
now() {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

} // namespace

int
main(int argc, char **argv) {
    opts o;
    int c;
    while ((c = getopt(argc, argv, "p:c:o:l:s:i:r:f:g:")) != -1) {
        switch (c) {
        case 'p': o.pool = optarg; break;
        case 'c': o.cont = optarg; break;
        case 'o': o.objs = atoi(optarg); break;
        case 'l': o.layers = atoi(optarg); break;
        case 's': o.lsize = strtoull(optarg, nullptr, 0); break;
        case 'i': o.inflight = atoi(optarg); break;
        case 'r': o.rounds = atoi(optarg); break;
        case 'f': o.fold = atoi(optarg); break;
        case 'g': o.gpu = atoi(optarg); break;
        default: return 2;
        }
    }

    nixl_b_params_t custom;
    nixlBackendInitParams p{};
    p.localAgent = "bench";
    p.type = "DAOS";
    p.customParams = &custom;
    nixlDaosEngine eng(&p);

    const double totalGiB =
        (double)o.objs * o.layers * o.lsize / (1024.0 * 1024 * 1024);
    printf("pool=%s cont=%s objs=%d layers=%d layer=%zu B inflight=%d fold=%d "
           "mem=%s total=%.2f GiB\n",
           o.pool.c_str(), o.cont.c_str(), o.objs, o.layers, o.lsize, o.inflight,
           o.fold, o.gpu ? "VRAM" : "DRAM", totalGiB);

#ifndef BENCH_HAVE_CUDA
    if (o.gpu) {
        fprintf(stderr, "built without CUDA: -g 1 unavailable\n");
        return 2;
    }
#endif
    const nixl_mem_t localSeg = o.gpu ? VRAM_SEG : DRAM_SEG;

    /* Register one DAOS object per "chunk". */
    std::vector<nixlBackendMD *> mds(o.objs, nullptr);
    for (int i = 0; i < o.objs; i++) {
        nixlBlobDesc d(0, 0, 9000 + i);
        d.metaInfo = o.pool + "/" + o.cont;
        if (eng.registerMem(d, FILE_SEG, mds[i]) != NIXL_SUCCESS || mds[i] == nullptr) {
            fprintf(stderr, "registerMem failed at object %d\n", i);
            return 1;
        }
    }

    /* One staging buffer per in-flight slot, reused across requests: a real
     * client holds its buffers, and reallocating per request churns addresses
     * in a way the fabric's MR cache does not like. */
    const size_t slotBytes = (size_t)o.layers * o.lsize;
    std::vector<std::vector<uint8_t>> hostBufs;
    std::vector<uint8_t *> slotBase(o.inflight, nullptr);
    nixlBackendMD *vmd = nullptr;

    if (o.gpu) {
#ifdef BENCH_HAVE_CUDA
        if (cudaSetDevice(0) != cudaSuccess) {
            fprintf(stderr, "cudaSetDevice failed\n");
            return 1;
        }
        nixlBlobDesc vdesc(0, slotBytes, 0);
        if (eng.registerMem(vdesc, VRAM_SEG, vmd) != NIXL_SUCCESS) {
            fprintf(stderr, "registerMem(VRAM_SEG) failed\n");
            return 1;
        }
        for (int i = 0; i < o.inflight; i++) {
            void *d = nullptr;
            if (cudaMalloc(&d, slotBytes) != cudaSuccess) {
                fprintf(stderr, "cudaMalloc(%zu) failed at slot %d\n", slotBytes, i);
                return 1;
            }
            cudaMemset(d, 0x5a, slotBytes);
            slotBase[i] = static_cast<uint8_t *>(d);
        }
        cudaDeviceSynchronize();
#endif
    } else {
        hostBufs.resize(o.inflight);
        for (int i = 0; i < o.inflight; i++) {
            hostBufs[i].assign(slotBytes, 0x5a);
            slotBase[i] = hostBufs[i].data();
        }
    }

    /* Build the descriptor lists once. With fold=1 every layer of an object
     * goes in one request, so prepXfer() can collapse them into a single
     * daos_obj_fetch(); with fold=0 each layer is its own request, which is
     * what a backend without folding would do. */
    struct unitDesc {
        nixl_meta_dlist_t loc;
        nixl_meta_dlist_t rem{FILE_SEG};
        explicit unitDesc(nixl_mem_t seg) : loc(seg) {}
    };
    std::vector<unitDesc> units;
    const int perObj = o.fold ? 1 : o.layers;
    units.reserve((size_t)o.objs * perObj);

    for (int i = 0; i < o.objs; i++) {
        if (o.fold) {
            unitDesc u(localSeg);
            for (int l = 0; l < o.layers; l++) {
                u.loc.addDesc(nixlMetaDesc(0, o.lsize, 0, vmd)); /* addr set later */
                u.rem.addDesc(
                    nixlMetaDesc((uintptr_t)l * o.lsize, o.lsize, 9000 + i, mds[i]));
            }
            units.push_back(std::move(u));
        } else {
            for (int l = 0; l < o.layers; l++) {
                unitDesc u(localSeg);
                u.loc.addDesc(nixlMetaDesc(0, o.lsize, 0, vmd));
                u.rem.addDesc(
                    nixlMetaDesc((uintptr_t)l * o.lsize, o.lsize, 9000 + i, mds[i]));
                units.push_back(std::move(u));
            }
        }
    }

    auto runAll = [&](nixl_xfer_op_t op) -> double {
        const double t0 = now();
        std::vector<nixlBackendReqH *> slots(o.inflight, nullptr);
        std::vector<int> slotUnit(o.inflight, -1);
        size_t next = 0;
        int live = 0;

        auto bindBufs = [&](unitDesc &u, int slot) {
            int k = 0;
            for (auto it = u.loc.begin(); it != u.loc.end(); ++it, ++k)
                const_cast<nixlMetaDesc &>(*it).addr =
                    (uintptr_t)(slotBase[slot] + (size_t)k * o.lsize);
        };

        while (next < units.size() || live > 0) {
            for (int s = 0; s < o.inflight && next < units.size(); s++) {
                if (slots[s] != nullptr) continue;
                bindBufs(units[next], s);
                nixlBackendReqH *h = nullptr;
                if (eng.prepXfer(op, units[next].loc, units[next].rem, "", h) !=
                    NIXL_SUCCESS) {
                    fprintf(stderr, "prepXfer failed\n");
                    exit(1);
                }
                const nixl_status_t st =
                    eng.postXfer(op, units[next].loc, units[next].rem, "", h);
                if (st != NIXL_IN_PROG && st != NIXL_SUCCESS) {
                    fprintf(stderr, "postXfer failed: %d\n", (int)st);
                    exit(1);
                }
                slots[s] = h;
                slotUnit[s] = (int)next;
                next++;
                live++;
            }
            for (int s = 0; s < o.inflight; s++) {
                if (slots[s] == nullptr) continue;
                const nixl_status_t st = eng.checkXfer(slots[s]);
                if (st == NIXL_IN_PROG) continue;
                if (st != NIXL_SUCCESS) {
                    fprintf(stderr, "transfer failed: %d (unit %d)\n", (int)st,
                            slotUnit[s]);
                    exit(1);
                }
                eng.releaseReqH(slots[s]);
                slots[s] = nullptr;
                live--;
            }
            if (live == o.inflight) std::this_thread::sleep_for(std::chrono::microseconds(50));
        }
        return now() - t0;
    };

    const double wt = runAll(NIXL_WRITE);
    printf("populate (write): %8.1f ms  %6.2f GB/s\n", wt * 1e3,
           totalGiB * 1.073741824 / wt);

    double best = 1e30;
    for (int r = 0; r < o.rounds; r++) {
        const double dt = runAll(NIXL_READ);
        if (dt < best) best = dt;
    }

    const long reqs = (long)units.size();
    const long descs = (long)o.objs * o.layers;
    printf("read: %8.1f ms | requests %5ld | per-request %7.3f ms | "
           "per-layer %7.3f ms | %6.2f GB/s\n",
           best * 1e3, reqs, best * 1e3 / reqs, best * 1e3 / descs,
           totalGiB * 1.073741824 / best);

#ifdef BENCH_HAVE_CUDA
    if (o.gpu) {
        for (int i = 0; i < o.inflight; i++) cudaFree(slotBase[i]);
        eng.deregisterMem(vmd);
    }
#endif
    for (auto *m : mds) eng.deregisterMem(m);
    return 0;
}
