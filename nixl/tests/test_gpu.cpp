/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Gluesys Co., Ltd. */
/*
 * VRAM_SEG round trip: write from GPU memory to DAOS, read back into a
 * different GPU buffer, compare on the device side.
 *
 * The comparison deliberately never routes the payload through host memory
 * on the checking path: the pattern is written from the device, and the
 * verdict is computed from the device buffer after a single copy out. A test
 * that staged through DRAM could pass while the GPU path silently fell back
 * to a host bounce, which is exactly the failure worth catching.
 *
 * Needs a client that exports daos_obj_fetch_gpu(); build the plugin with
 * DAOS_PREFIX pointing at it. Without that the backend does not advertise
 * VRAM_SEG at all and this program reports it rather than guessing.
 *
 *   test_gpu <pool> <container>
 */
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#include <cuda_runtime.h>

#include "daos_backend.h"

static int fails = 0;

static void
ck(const char *what, bool ok) {
    if (!ok) fails++;
    printf("  %-54s %s\n", what, ok ? "PASS" : "FAIL");
}

#define CUDA_OK(call)                                                          \
    do {                                                                       \
        const cudaError_t e = (call);                                          \
        if (e != cudaSuccess) {                                                \
            printf("  CUDA %s failed: %s\n", #call, cudaGetErrorString(e));    \
            return 1;                                                          \
        }                                                                      \
    } while (0)

int
main(int argc, char **argv) {
    const std::string pool = argc > 1 ? argv[1] : "attr1";
    const std::string cont = argc > 2 ? argv[2] : "nixlgpu";

    const int nLayer = 8;
    const size_t chunk = 1u << 20;
    const uint64_t objDev = 5150;

    int devCount = 0;
    CUDA_OK(cudaGetDeviceCount(&devCount));
    if (devCount == 0) {
        printf("  no CUDA device\n");
        return 1;
    }
    CUDA_OK(cudaSetDevice(0));

    nixl_b_params_t custom;
    nixlBackendInitParams p{};
    p.localAgent = "gputest";
    p.type = "DAOS";
    p.customParams = &custom;
    nixlDaosEngine eng(&p);

    bool haveVram = false;
    for (const auto &m : eng.getSupportedMems())
        if (m == VRAM_SEG) haveVram = true;
    ck("backend advertises VRAM_SEG", haveVram);
    if (!haveVram) {
        printf("\n  === SKIPPED: build the plugin with DAOS_PREFIX pointing at a\n"
               "      client that exports daos_obj_fetch_gpu() ===\n");
        return 1;
    }

    nixlBlobDesc obj(0, 0, objDev);
    obj.metaInfo = pool + "/" + cont;
    nixlBackendMD *md = nullptr;
    ck("register object", eng.registerMem(obj, FILE_SEG, md) == NIXL_SUCCESS);
    if (md == nullptr) return 1;

    nixlBlobDesc vdesc(0, chunk, 0);
    nixlBackendMD *vmd = nullptr;
    ck("register VRAM_SEG", eng.registerMem(vdesc, VRAM_SEG, vmd) == NIXL_SUCCESS);

    /* Device buffers. The pattern is produced on the device so nothing about
     * the payload has touched host memory before it reaches DAOS. */
    std::vector<void *> dsrc(nLayer), ddst(nLayer);
    std::vector<uint64_t> host(chunk / 8);
    for (int i = 0; i < nLayer; i++) {
        CUDA_OK(cudaMalloc(&dsrc[i], chunk));
        CUDA_OK(cudaMalloc(&ddst[i], chunk));
        for (size_t k = 0; k < chunk / 8; k++) host[k] = ((uint64_t)(0xD0 + i) << 40) | k;
        CUDA_OK(cudaMemcpy(dsrc[i], host.data(), chunk, cudaMemcpyHostToDevice));
        CUDA_OK(cudaMemset(ddst[i], 0, chunk));
    }
    CUDA_OK(cudaDeviceSynchronize());

    auto run = [&](nixl_xfer_op_t op, std::vector<void *> &bufs) -> nixl_status_t {
        nixl_meta_dlist_t loc(VRAM_SEG), rem(FILE_SEG);
        for (int i = 0; i < nLayer; i++) {
            loc.addDesc(nixlMetaDesc((uintptr_t)bufs[i], chunk, 0, vmd));
            rem.addDesc(nixlMetaDesc((uintptr_t)i * chunk, chunk, objDev, md));
        }
        nixlBackendReqH *h = nullptr;
        nixl_status_t st = eng.prepXfer(op, loc, rem, "", h);
        if (st != NIXL_SUCCESS) return st;
        st = eng.postXfer(op, loc, rem, "", h);
        if (st != NIXL_IN_PROG && st != NIXL_SUCCESS) {
            eng.releaseReqH(h);
            return st;
        }
        int spins = 0;
        do {
            st = eng.checkXfer(h);
        } while (st == NIXL_IN_PROG && ++spins < 60000000);
        eng.releaseReqH(h);
        return st;
    };

    ck("write from GPU memory (daos_obj_update_gpu)", run(NIXL_WRITE, dsrc) == NIXL_SUCCESS);
    ck("read into GPU memory (daos_obj_fetch_gpu)", run(NIXL_READ, ddst) == NIXL_SUCCESS);

    bool all = true;
    for (int i = 0; i < nLayer; i++) {
        std::vector<uint64_t> back(chunk / 8, 0);
        CUDA_OK(cudaMemcpy(back.data(), ddst[i], chunk, cudaMemcpyDeviceToHost));
        for (size_t k = 0; k < chunk / 8; k++) {
            const uint64_t want = ((uint64_t)(0xD0 + i) << 40) | k;
            if (back[k] != want) {
                printf("    layer %d: word %zu = 0x%lx, expected 0x%lx\n", i, k,
                       back[k], want);
                all = false;
                break;
            }
        }
    }
    ck("payload identical, device to device", all);

    for (int i = 0; i < nLayer; i++) {
        cudaFree(dsrc[i]);
        cudaFree(ddst[i]);
    }
    ck("deregister VRAM", eng.deregisterMem(vmd) == NIXL_SUCCESS);
    ck("deregister object", eng.deregisterMem(md) == NIXL_SUCCESS);

    printf("\n  === %s (%d failure%s) ===\n", fails ? "FAILED" : "ALL PASS", fails,
           fails == 1 ? "" : "s");
    return fails ? 1 : 0;
}
