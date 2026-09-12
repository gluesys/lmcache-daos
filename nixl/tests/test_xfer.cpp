/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Gluesys Co., Ltd. */
/*
 * Write-then-read round trip through the DAOS backend, driven directly so a
 * failure points at the backend rather than at NIXL's agent plumbing.
 *
 * The payload is self-describing -- every 8-byte word encodes its own
 * (descriptor, offset) -- so a region that comes back wrong names where it
 * actually came from instead of just failing a memcmp. This project has been
 * bitten by silent cross-chunk corruption before; a plain pattern would have
 * hidden it.
 */
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <thread>
#include <chrono>

#include "daos_backend.h"

static int fails = 0;

static void
ck(const char *what, bool ok) {
    if (!ok) fails++;
    printf("  %-52s %s\n", what, ok ? "PASS" : "FAIL");
}

static void
fill(uint64_t *p, size_t bytes, uint64_t tag) {
    for (size_t i = 0; i < bytes / 8; i++) p[i] = (tag << 40) | i;
}

static size_t
firstBad(const uint64_t *p, size_t bytes, uint64_t tag) {
    for (size_t i = 0; i < bytes / 8; i++)
        if (p[i] != ((tag << 40) | i)) return i;
    return SIZE_MAX;
}

static nixl_status_t
runXfer(nixlDaosEngine &eng,
        nixl_xfer_op_t op,
        const nixl_meta_dlist_t &loc,
        const nixl_meta_dlist_t &rem) {
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
        if (st == NIXL_IN_PROG) {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            spins++;
        }
    } while (st == NIXL_IN_PROG && spins < 30000);

    eng.releaseReqH(h);
    return st;
}

int
main(int argc, char **argv) {
    const std::string pool = argc > 1 ? argv[1] : "kvpool";
    const std::string cont = argc > 2 ? argv[2] : "nixltest";

    const int nDesc = 8;             /* 8 akeys under one dkey -> one RPC */
    const size_t chunk = 256 * 1024; /* 2 MiB total, fits the 6.5 GB pool */

    nixl_b_params_t custom;
    nixlBackendInitParams p{};
    p.localAgent = "test";
    p.type = "DAOS";
    p.customParams = &custom;
    nixlDaosEngine eng(&p);

    nixlBlobDesc obj(0, 0, 4242);
    obj.metaInfo = pool + "/" + cont;
    nixlBackendMD *md = nullptr;
    ck("register object", eng.registerMem(obj, FILE_SEG, md) == NIXL_SUCCESS);
    if (md == nullptr) {
        printf("\n  === FAILED (cannot continue) ===\n");
        return 1;
    }

    std::vector<std::vector<uint64_t>> wbuf(nDesc), rbuf(nDesc);
    nixl_meta_dlist_t wloc(DRAM_SEG), rloc(DRAM_SEG), rem(FILE_SEG);

    for (int i = 0; i < nDesc; i++) {
        wbuf[i].resize(chunk / 8);
        rbuf[i].assign(chunk / 8, 0);
        fill(wbuf[i].data(), chunk, 0xA0 + i);

        wloc.addDesc(nixlMetaDesc(reinterpret_cast<uintptr_t>(wbuf[i].data()), chunk, 0, nullptr));
        rloc.addDesc(nixlMetaDesc(reinterpret_cast<uintptr_t>(rbuf[i].data()), chunk, 0, nullptr));
        /* offsets inside one 64 MiB dkey span, so all 8 fold into one RPC */
        rem.addDesc(nixlMetaDesc(static_cast<uintptr_t>(i) * chunk, chunk, 4242, md));
    }

    ck("write 8 descriptors", runXfer(eng, NIXL_WRITE, wloc, rem) == NIXL_SUCCESS);
    ck("read them back", runXfer(eng, NIXL_READ, rloc, rem) == NIXL_SUCCESS);

    bool all = true;
    for (int i = 0; i < nDesc; i++) {
        const size_t bad = firstBad(rbuf[i].data(), chunk, 0xA0 + i);
        if (bad != SIZE_MAX) {
            all = false;
            printf("    desc %d: word %zu = 0x%lx, expected 0x%lx\n",
                   i, bad, rbuf[i][bad], (uint64_t)((0xA0 + i) << 40 | bad));
        }
    }
    ck("payload identical, every descriptor", all);

    /* A key never written must be reported as a miss, not as zeros. */
    std::vector<uint64_t> miss(chunk / 8, 0);
    nixl_meta_dlist_t mloc(DRAM_SEG), mrem(FILE_SEG);
    mloc.addDesc(nixlMetaDesc(reinterpret_cast<uintptr_t>(miss.data()), chunk, 0, nullptr));
    mrem.addDesc(nixlMetaDesc(999ull * chunk, chunk, 4242, md));
    ck("read of an unwritten key is NOT silently empty",
       runXfer(eng, NIXL_READ, mloc, mrem) != NIXL_SUCCESS);

    /* Offsets far apart land in different dkeys: still correct, more RPCs. */
    std::vector<uint64_t> sw(chunk / 8), sr(chunk / 8, 0);
    fill(sw.data(), chunk, 0xBB);
    nixl_meta_dlist_t sloc(DRAM_SEG), srloc(DRAM_SEG), srem(FILE_SEG);
    sloc.addDesc(nixlMetaDesc(reinterpret_cast<uintptr_t>(sw.data()), chunk, 0, nullptr));
    srloc.addDesc(nixlMetaDesc(reinterpret_cast<uintptr_t>(sr.data()), chunk, 0, nullptr));
    srem.addDesc(nixlMetaDesc(300ull << 20, chunk, 4242, md)); /* dkey 4 */
    ck("write across a dkey boundary", runXfer(eng, NIXL_WRITE, sloc, srem) == NIXL_SUCCESS);
    ck("read it back", runXfer(eng, NIXL_READ, srloc, srem) == NIXL_SUCCESS);
    ck("payload identical across dkey boundary",
       firstBad(sr.data(), chunk, 0xBB) == SIZE_MAX);

    ck("mismatched descriptor counts rejected", [&] {
        nixlBackendReqH *h = nullptr;
        nixl_meta_dlist_t one(DRAM_SEG);
        one.addDesc(nixlMetaDesc(reinterpret_cast<uintptr_t>(sw.data()), chunk, 0, nullptr));
        return eng.prepXfer(NIXL_READ, one, rem, "", h) == NIXL_ERR_INVALID_PARAM;
    }());

    ck("deregister", eng.deregisterMem(md) == NIXL_SUCCESS);

    printf("\n  === %s (%d failure%s) ===\n", fails ? "FAILED" : "ALL PASS", fails,
           fails == 1 ? "" : "s");
    return fails ? 1 : 0;
}
