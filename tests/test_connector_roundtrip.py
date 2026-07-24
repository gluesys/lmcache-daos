"""Real connector-level roundtrip against a live DAOS pool (T2).

Requires LMCache installed + a DAOS client (agent) + a POSIX container.
    DAOS_TEST_POOL=lmcache DAOS_TEST_CONT=lmcache_test \
        python3 tests/test_connector_roundtrip.py

Exercises the full path: LocalCPUBackend.allocate -> fill bytes ->
DaosConnector.put (RemoteMetadata.serialize + DFS write) -> exists ->
get (DFS read + RemoteMetadata.deserialize + allocate + copy) -> compare.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POOL = os.environ.get("DAOS_TEST_POOL", "lmcache")
CONT = os.environ.get("DAOS_TEST_CONT", "lmcache_test")


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
        model_name="test-model",
        world_size=1,
        local_world_size=1,
        worker_id=0,
        local_worker_id=0,
        kv_dtype=torch.bfloat16,
        kv_shape=(2, 256, 8, 1, 8),
    )
    lcb = LocalCPUBackend(config, metadata)

    # allocate a small KV-like MemoryObj and fill with a known pattern
    shape = torch.Size([2, 4, 8])
    dtype = torch.bfloat16
    mo = lcb.allocate(shape, dtype, MemoryFormat.KV_2LTD)
    assert mo is not None, "LocalCPUBackend.allocate returned None"

    view = mo.byte_array
    if isinstance(view, memoryview) and view.format == "<B":
        view = view.cast("B")
    nbytes = len(view)
    pattern = bytes((i * 7 + 3) & 0xFF for i in range(nbytes))
    view[:nbytes] = pattern
    print(f"allocated MemoryObj: {nbytes} bytes, shape={list(shape)}, dtype={dtype}")

    key = CacheEngineKey("test-model", 1, 0, 0xDEADBEEF, dtype)

    loop = asyncio.new_event_loop()
    conn = DaosConnector(f"daos://{POOL}/{CONT}", loop, lcb)
    try:
        assert not loop.run_until_complete(conn.exists(key)), "key should not exist yet"
        loop.run_until_complete(conn.put(key, mo))
        print("put OK")
        assert loop.run_until_complete(conn.exists(key)), "key should exist after put"
        assert conn.exists_sync(key), "exists_sync should be True"
        got = loop.run_until_complete(conn.get(key))
        assert got is not None, "get returned None"

        gview = got.byte_array
        if isinstance(gview, memoryview) and gview.format == "<B":
            gview = gview.cast("B")
        assert bytes(gview[:nbytes]) == pattern, "payload mismatch after roundtrip"
        assert got.get_shapes() == mo.get_shapes(), "shape mismatch"
        assert got.get_dtypes() == mo.get_dtypes(), "dtype mismatch"
        print("get + compare OK (payload, shape, dtype all match)")
    finally:
        loop.run_until_complete(conn.close())
        loop.close()
    print("PASS connector roundtrip (put/exists/get/compare) via DAOS")


if __name__ == "__main__":
    main()
