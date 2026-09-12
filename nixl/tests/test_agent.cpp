/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Gluesys Co., Ltd. */
/*
 * The DAOS backend under a real nixlAgent, rather than driven directly.
 *
 * Everything measured so far bypassed the agent, which means one assumption
 * was never tested: that the descriptor lists prepXfer() receives are shaped
 * the way the backend expects. That shape -- local is DRAM, remote is storage,
 * paired by index, remote->addr is the offset -- was inferred from reading
 * posix_backend.cpp, not from any stated contract. If the agent produces
 * something else, the folding in prepXfer() is wrong and every number taken
 * so far describes a path nobody will use.
 *
 * The agent also differs in a way that matters: registerMem() takes
 * nixlBlobDesc (carrying metaInfo), but createXferReq() takes nixlBasicDesc
 * with no metadata pointer at all. The agent has to match a transfer
 * descriptor back to a registered region by (addr, len, devId) on its own.
 * Whether devId alone is enough to name our object is exactly what this
 * checks.
 *
 *   test_agent <pool> <container>
 */
#include <cstdint>
#include <cstdio>
#include <string>
#include <thread>
#include <chrono>
#include <vector>

#include "nixl.h"

static int fails = 0;

static void
ck(const char *what, bool ok) {
    if (!ok) fails++;
    printf("  %-54s %s\n", what, ok ? "PASS" : "FAIL");
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

int
main(int argc, char **argv) {
    const std::string pool = argc > 1 ? argv[1] : "attr1";
    const std::string cont = argc > 2 ? argv[2] : "nixlagent";

    const int nLayer = 8;
    const size_t chunk = 256 * 1024;
    const uint64_t objDev = 7777;

    nixlAgentConfig cfg;
    nixlAgent agent("agent-A", cfg);

    std::vector<nixl_backend_t> plugins;
    ck("getAvailPlugins", agent.getAvailPlugins(plugins) == NIXL_SUCCESS);
    bool haveDaos = false;
    for (auto &p : plugins)
        if (p == "DAOS") haveDaos = true;
    printf("  plugins:");
    for (auto &p : plugins) printf(" %s", p.c_str());
    printf("\n");
    ck("DAOS plugin is discovered by the agent", haveDaos);
    if (!haveDaos) {
        printf("\n  === FAILED (plugin not found; set NIXL_PLUGIN_DIR) ===\n");
        return 1;
    }

    nixl_b_params_t params;
    nixlBackendH *bh = nullptr;
    ck("createBackend(DAOS)", agent.createBackend("DAOS", params, bh) == NIXL_SUCCESS);
    if (bh == nullptr) {
        printf("\n  === FAILED (no backend handle) ===\n");
        return 1;
    }

    /* Register the DAOS object (FILE_SEG) and the DRAM buffers separately,
     * the way an application would. */
    std::vector<std::vector<uint64_t>> wbuf(nLayer), rbuf(nLayer);
    for (int i = 0; i < nLayer; i++) {
        wbuf[i].resize(chunk / 8);
        rbuf[i].assign(chunk / 8, 0);
        fill(wbuf[i].data(), chunk, 0xC0 + i);
    }

    nixl_reg_dlist_t fileReg(FILE_SEG);
    nixlBlobDesc objDesc(0, 0, objDev);
    objDesc.metaInfo = pool + "/" + cont;
    fileReg.addDesc(objDesc);

    nixl_reg_dlist_t dramReg(DRAM_SEG);
    for (int i = 0; i < nLayer; i++) {
        dramReg.addDesc(nixlBlobDesc((uintptr_t)wbuf[i].data(), chunk, 0));
        dramReg.addDesc(nixlBlobDesc((uintptr_t)rbuf[i].data(), chunk, 0));
    }

    nixl_opt_args_t ext;
    ext.backends.push_back(bh);

    ck("registerMem(FILE_SEG)", agent.registerMem(fileReg, &ext) == NIXL_SUCCESS);
    ck("registerMem(DRAM_SEG)", agent.registerMem(dramReg, &ext) == NIXL_SUCCESS);

    auto runXfer = [&](nixl_xfer_op_t op,
                       std::vector<std::vector<uint64_t>> &bufs) -> nixl_status_t {
        nixl_xfer_dlist_t loc(DRAM_SEG), rem(FILE_SEG);
        for (int i = 0; i < nLayer; i++) {
            loc.addDesc(nixlBasicDesc((uintptr_t)bufs[i].data(), chunk, 0));
            rem.addDesc(nixlBasicDesc((uintptr_t)i * chunk, chunk, objDev));
        }
        nixlXferReqH *req = nullptr;
        nixl_status_t st = agent.createXferReq(op, loc, rem, "agent-A", req, &ext);
        if (st != NIXL_SUCCESS) {
            printf("    createXferReq -> %d\n", (int)st);
            return st;
        }
        st = agent.postXferReq(req);
        int spins = 0;
        while (st == NIXL_IN_PROG && spins++ < 30000) {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            st = agent.getXferStatus(req);
        }
        agent.releaseXferReq(req);
        return st;
    };

    ck("createXferReq + postXferReq (WRITE)", runXfer(NIXL_WRITE, wbuf) == NIXL_SUCCESS);
    ck("createXferReq + postXferReq (READ)", runXfer(NIXL_READ, rbuf) == NIXL_SUCCESS);

    bool all = true;
    for (int i = 0; i < nLayer; i++) {
        const size_t bad = firstBad(rbuf[i].data(), chunk, 0xC0 + i);
        if (bad != SIZE_MAX) {
            all = false;
            printf("    layer %d: word %zu = 0x%lx, expected 0x%lx\n", i, bad,
                   rbuf[i][bad], (uint64_t)((0xC0 + i) << 40 | bad));
        }
    }
    ck("payload identical through the agent", all);

    ck("deregisterMem(DRAM_SEG)", agent.deregisterMem(dramReg, &ext) == NIXL_SUCCESS);
    ck("deregisterMem(FILE_SEG)", agent.deregisterMem(fileReg, &ext) == NIXL_SUCCESS);

    printf("\n  === %s (%d failure%s) ===\n", fails ? "FAILED" : "ALL PASS", fails,
           fails == 1 ? "" : "s");
    return fails ? 1 : 0;
}
