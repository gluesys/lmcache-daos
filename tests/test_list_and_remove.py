# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Enumeration and deletion: list() and remove_sync() (T7).

These two are what capacity management is built on. Before this, `list()`
returned `[]` and there was no delete path at all, so a container could only
grow -- and with a 32K-token prefix costing ~3.5 GiB that fills a pool almost
immediately.

`RemoteBackend.remove()` calls `remove_sync()`, so implementing it is what makes
LMCache's remote eviction functional.

    DAOS_TEST_POOL=nvme_pool DAOS_TEST_CONT=lmcache_nvme \
        python tests/test_list_and_remove.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POOL = os.environ.get("DAOS_TEST_POOL", "nvme_pool")
CONT = os.environ.get("DAOS_TEST_CONT", "lmcache_nvme")
N = int(os.environ.get("N_OBJECTS", "25"))


def main():
    import torch
    from lmcache.v1.config import LMCacheEngineConfig
    from lmcache.v1.metadata import LMCacheMetadata
    from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend
    from lmcache.v1.memory_management import MemoryFormat
    from lmcache.utils import CacheEngineKey
    from lmcache_daos.connector import DaosConnector, _key_to_path

    config = LMCacheEngineConfig.from_defaults()
    metadata = LMCacheMetadata(
        model_name="test-model", world_size=1, local_world_size=1,
        worker_id=0, local_worker_id=0, kv_dtype=torch.bfloat16,
        kv_shape=(2, 256, 8, 1, 8),
    )
    lcb = LocalCPUBackend(config, metadata)
    loop = asyncio.new_event_loop()
    conn = DaosConnector(f"daos://{POOL}/{CONT}", loop, lcb)

    shape, dtype = torch.Size([2, 64, 8]), torch.bfloat16

    def obj(seed):
        mo = lcb.allocate(shape, dtype, MemoryFormat.KV_2LTD)
        assert mo is not None
        v = mo.byte_array
        if isinstance(v, memoryview) and v.format == "<B":
            v = v.cast("B")
        v[: len(v)] = bytes((i + seed) & 0xFF for i in range(len(v)))
        return mo

    keys = [CacheEngineKey("test-model", 1, 0, 0x11570000 + i, dtype)
            for i in range(N)]
    paths = {_key_to_path(k).lstrip("/") for k in keys}
    rc = 0
    try:
        # Start from a known state for just our own keys (the container is
        # shared with the other tests, so never wipe it wholesale).
        for k in keys:
            conn.remove_sync(k)

        before = set(loop.run_until_complete(conn.list()))
        print(f"container has {len(before)} objects before")
        assert not (before & paths), "our keys should not be present yet"

        for i, k in enumerate(keys):
            loop.run_until_complete(conn.put(k, obj(i)))
        after = set(loop.run_until_complete(conn.list()))
        print(f"container has {len(after)} objects after storing {N}")

        missing = paths - after
        if missing:
            print(f"FAIL list() missed {len(missing)}/{N} stored objects")
            return 1
        if len(after) != len(before) + N:
            print(f"FAIL expected {len(before) + N} objects, list() gave {len(after)}")
            return 1
        print(f"list() reported all {N} stored objects (+{len(after) - len(before)})")

        # remove_sync must report True for a present key, False for an absent one
        k0 = keys[0]
        if not conn.remove_sync(k0):
            print("FAIL remove_sync returned False for an existing key")
            rc = 1
        if conn.remove_sync(k0):
            print("FAIL remove_sync returned True for an already-deleted key")
            rc = 1
        if loop.run_until_complete(conn.exists(k0)):
            print("FAIL key still exists after remove_sync")
            rc = 1
        gone = set(loop.run_until_complete(conn.list()))
        if _key_to_path(k0).lstrip("/") in gone:
            print("FAIL list() still reports the removed object")
            rc = 1
        print("remove_sync: True on present, False on absent, gone from list()")

        # sweep the rest and confirm the container returns to its prior size
        for k in keys[1:]:
            conn.remove_sync(k)
        end = set(loop.run_until_complete(conn.list()))
        if end & paths:
            print(f"FAIL {len(end & paths)} of our objects survived the sweep")
            rc = 1
        if len(end) != len(before):
            print(f"FAIL container size {len(end)} != original {len(before)}")
            rc = 1
        print(f"swept all {N}; container back to {len(end)} objects")
    finally:
        for k in keys:
            try:
                conn.remove_sync(k)
            except Exception:
                pass
        loop.run_until_complete(conn.close())
        loop.close()

    if rc == 0:
        print("\nPASS list() + remove_sync()")
    else:
        print("\nFAIL list()/remove_sync()")
    return rc


if __name__ == "__main__":
    sys.exit(main())
