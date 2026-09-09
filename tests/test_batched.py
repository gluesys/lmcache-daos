# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Batched interface: get / put, plus the inherited prefix semantics (T8).

Only batched_get and batched_put are overridden (measured 1.3-3.5x). This test
also pins down two things that are easy to get wrong:

  * `support_batched_contains()` must stay False. Overriding batched_contains
    measured *slower* than the inherited sequential loop (9.4 vs 8.8 ms for 128
    keys) because dfs_sys_open is only ~69 us. The assertion here is a guard
    against someone "optimising" it back in without new data.
  * `batched_async_contains` (inherited) returns a *consecutive prefix* count,
    not a total. KV reuse only works on a contiguous prefix -- chunk 5 is
    useless if chunk 3 is gone -- so a hole must truncate the answer even when
    later keys are present. batched_get, by contrast, is positional.

    DAOS_TEST_POOL=nvme_pool DAOS_TEST_CONT=lmcache_nvme \
        python tests/test_batched.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POOL = os.environ.get("DAOS_TEST_POOL", "nvme_pool")
CONT = os.environ.get("DAOS_TEST_CONT", "lmcache_nvme")
N = int(os.environ.get("N_CHUNKS", "48"))


def main():
    import torch
    from lmcache.v1.config import LMCacheEngineConfig
    from lmcache.v1.metadata import LMCacheMetadata
    from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend
    from lmcache.v1.memory_management import MemoryFormat
    from lmcache.utils import CacheEngineKey
    from lmcache_daos.connector import DaosConnector

    config = LMCacheEngineConfig.from_defaults()
    metadata = LMCacheMetadata(
        model_name="test-model", world_size=1, local_world_size=1,
        worker_id=0, local_worker_id=0, kv_dtype=torch.bfloat16,
        kv_shape=(2, 256, 8, 1, 8),
    )
    lcb = LocalCPUBackend(config, metadata)
    loop = asyncio.new_event_loop()
    conn = DaosConnector(f"daos://{POOL}/{CONT}", loop, lcb)

    shape, dtype = torch.Size([2, 128, 8]), torch.bfloat16

    def obj(seed):
        mo = lcb.allocate(shape, dtype, MemoryFormat.KV_2LTD)
        assert mo is not None
        v = mo.byte_array
        if isinstance(v, memoryview) and v.format == "<B":
            v = v.cast("B")
        v[: len(v)] = bytes((i * 3 + seed) & 0xFF for i in range(len(v)))
        return mo, bytes(v[: len(v)])

    keys = [CacheEngineKey("test-model", 1, 0, 0xBA7C0000 + i, dtype)
            for i in range(N)]
    rc = 0
    try:
        for k in keys:
            conn.remove_sync(k)

        print(f"support: get={conn.support_batched_get()} "
              f"put={conn.support_batched_put()} "
              f"contains={conn.support_batched_contains()} (intentionally False)")
        if conn.support_batched_get() is not True or conn.support_batched_put() is not True:
            print("FAIL batched get/put should be advertised as supported"); rc = 1
        if conn.support_batched_contains() is not False:
            print("FAIL support_batched_contains must stay False -- overriding it "
                  "measured slower than the inherited sequential loop"); rc = 1

        # --- empty container: prefix count must be 0, not N ---
        n = loop.run_until_complete(conn.batched_async_contains("l0", keys))
        if n != 0:
            print(f"FAIL batched_async_contains on empty != 0 (got {n})"); rc = 1

        # --- batched_put then full prefix ---
        objs, expected = zip(*(obj(i) for i in range(N)))
        t0 = time.time()
        loop.run_until_complete(conn.batched_put(list(keys), list(objs)))
        t_put = time.time() - t0
        print(f"batched_put {N} objects in {t_put*1000:.1f} ms")

        got_a = loop.run_until_complete(conn.batched_async_contains("l1", keys))
        if got_a != N:
            print(f"FAIL batched_async_contains after put: {got_a} != {N}"); rc = 1
        print(f"batched_async_contains reports the full prefix ({N})")

        # --- batched_get: positional, payload must match ---
        t0 = time.time()
        mos = loop.run_until_complete(conn.batched_get(list(keys)))
        t_get = time.time() - t0
        if len(mos) != N:
            print(f"FAIL batched_get length {len(mos)} != {N}"); rc = 1
        bad = 0
        for i, mo in enumerate(mos):
            if mo is None:
                bad += 1; continue
            g = mo.byte_array
            if isinstance(g, memoryview) and g.format == "<B":
                g = g.cast("B")
            if bytes(g[: len(expected[i])]) != expected[i]:
                bad += 1
        if bad:
            print(f"FAIL batched_get: {bad}/{N} wrong or missing"); rc = 1
        else:
            print(f"batched_get {N} objects in {t_get*1000:.1f} ms, all payloads match")

        # --- a hole must truncate the prefix count ---
        hole = N // 3
        conn.remove_sync(keys[hole])
        got_a = loop.run_until_complete(conn.batched_async_contains("l2", keys))
        if got_a != hole:
            print(f"FAIL async prefix truncation: {got_a} != {hole}"); rc = 1
        # ...while batched_get is positional and still returns the tail
        mos = loop.run_until_complete(conn.batched_get(list(keys)))
        if mos[hole] is not None:
            print("FAIL batched_get should report None at the hole"); rc = 1
        if mos[hole + 1] is None:
            print("FAIL batched_get should still return keys after the hole"); rc = 1
        if rc == 0:
            print(f"hole at index {hole}: prefix count truncates to {hole}, "
                  f"batched_get stays positional")

        # --- absent keys mixed in ---
        absent = [CacheEngineKey("test-model", 1, 0, 0xDEAD0000 + i, dtype)
                  for i in range(4)]
        mos = loop.run_until_complete(conn.batched_get(absent))
        if any(m is not None for m in mos):
            print("FAIL batched_get returned an object for an absent key"); rc = 1
        else:
            print("batched_get returns None for absent keys")
    finally:
        for k in keys:
            try:
                conn.remove_sync(k)
            except Exception:
                pass
        loop.run_until_complete(conn.close())
        loop.close()

    print("\n" + ("PASS batched get/put + inherited prefix semantics" if rc == 0
                  else "FAIL batched interface"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
