/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Gluesys Co., Ltd. */
/* registerMem/deregisterMem against a live DAOS pool, driven directly (no
 * agent) so a failure points at the backend rather than at NIXL plumbing. */
#include <cstdio>
#include <string>
#include "daos_backend.h"

static int fails = 0;
static void ck(const char *what, nixl_status_t got, nixl_status_t want) {
    bool ok = (got == want);
    if (!ok) fails++;
    printf("  %-46s got=%d want=%d  %s\n", what, (int)got, (int)want, ok ? "PASS" : "FAIL");
}

int main(int argc, char **argv) {
    const std::string pool = argc > 1 ? argv[1] : "kvpool";
    const std::string cont = argc > 2 ? argv[2] : "nixltest";

    /* nixlBackendEngine's constructor dereferences customParams without a null
     * check, so it must point at something even when empty. */
    nixl_b_params_t custom;
    nixlBackendInitParams p{};
    p.localAgent = "test";
    p.type = "DAOS";
    p.customParams = &custom;
    nixlDaosEngine eng(&p);

    nixlBackendMD *md1 = nullptr, *md2 = nullptr, *mdd = nullptr;

    nixlBlobDesc dram(0, 4096, 0);
    ck("DRAM_SEG register (no metadata expected)",
       eng.registerMem(dram, DRAM_SEG, mdd), NIXL_SUCCESS);
    printf("  %-46s %s\n", "DRAM metadata is null", mdd == nullptr ? "PASS" : "FAIL");
    if (mdd != nullptr) fails++;

    nixlBlobDesc bad(0, 0, 1);
    bad.metaInfo = "onlypool";
    nixlBackendMD *mdb = nullptr;
    ck("malformed metaInfo rejected", eng.registerMem(bad, FILE_SEG, mdb), NIXL_ERR_INVALID_PARAM);

    nixlBlobDesc o1(0, 0, 1001);
    o1.metaInfo = pool + "/" + cont;
    ck("register object devId=1001", eng.registerMem(o1, FILE_SEG, md1), NIXL_SUCCESS);

    nixlBlobDesc o2(0, 0, 1002);
    o2.metaInfo = pool + "/" + cont;
    ck("register object devId=1002 (same container)",
       eng.registerMem(o2, FILE_SEG, md2), NIXL_SUCCESS);

    if (md1 && md2) {
        auto *a = dynamic_cast<nixlDaosObjMD *>(md1);
        auto *b = dynamic_cast<nixlDaosObjMD *>(md2);
        printf("  oid(1001) = %lu.%lu\n", a->oid_.hi, a->oid_.lo);
        printf("  oid(1002) = %lu.%lu\n", b->oid_.hi, b->oid_.lo);
        bool distinct = !(a->oid_.hi == b->oid_.hi && a->oid_.lo == b->oid_.lo);
        printf("  %-46s %s\n", "distinct devId -> distinct oid", distinct ? "PASS" : "FAIL");
        if (!distinct) fails++;
    }

    ck("deregister 1001", eng.deregisterMem(md1), NIXL_SUCCESS);
    ck("deregister 1002 (closes container)", eng.deregisterMem(md2), NIXL_SUCCESS);
    ck("deregister null (DRAM) is a no-op", eng.deregisterMem(nullptr), NIXL_SUCCESS);

    /* Reopening after the refcount hit zero proves putCont() did not leave a
     * stale handle behind. */
    nixlBackendMD *md3 = nullptr;
    nixlBlobDesc o3(0, 0, 1001);
    o3.metaInfo = pool + "/" + cont;
    ck("re-register 1001 after container closed",
       eng.registerMem(o3, FILE_SEG, md3), NIXL_SUCCESS);
    if (md3) {
        auto *c = dynamic_cast<nixlDaosObjMD *>(md3);
        printf("  oid(1001) again = %lu.%lu\n", c->oid_.hi, c->oid_.lo);
    }
    ck("deregister again", eng.deregisterMem(md3), NIXL_SUCCESS);

    printf("\n  === %s (%d failure%s) ===\n", fails ? "FAILED" : "ALL PASS", fails,
           fails == 1 ? "" : "s");
    return fails ? 1 : 0;
}
