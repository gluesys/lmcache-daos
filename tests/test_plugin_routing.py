# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""End-to-end plugin routing through LMCache's own connector factory (T3).

This is the piece the README listed as unverified: does LMCache actually route
our out-of-tree connector, and under which url spelling?

Answer (measured against LMCache 0.5.2): the plugin adapter's schema is
``plugin://<plugin_type>`` and ``can_parse`` is a ``startswith`` test, so the
url must be ``plugin://daos/<pool>/<container>``. A plain ``daos://...`` url is
NOT matched by any adapter and CreateConnector raises
"No adapter found for URL".

    DAOS_TEST_POOL=hdd_pool DAOS_TEST_CONT=lmcache_test \
        python tests/test_plugin_routing.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POOL = os.environ.get("DAOS_TEST_POOL", "lmcache")
CONT = os.environ.get("DAOS_TEST_CONT", "lmcache_test")


def build_config(remote_url):
    from lmcache.v1.config import LMCacheEngineConfig

    return LMCacheEngineConfig.from_defaults(
        remote_url=remote_url,
        remote_serde="naive",
        remote_storage_plugins=["daos"],
        extra_config={
            "remote_storage_plugin.daos.module_path": "lmcache_daos.connector",
            "remote_storage_plugin.daos.class_name": "DaosConnector",
        },
    )


def main():
    import torch
    from lmcache.v1.metadata import LMCacheMetadata
    from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend
    from lmcache.v1.memory_management import MemoryFormat
    from lmcache.utils import CacheEngineKey
    from lmcache.v1.storage_backend.connector import CreateConnector

    url = f"plugin://daos/{POOL}/{CONT}"
    config = build_config(url)
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
    loop = asyncio.new_event_loop()

    # 1) the negative control: the old daos:// spelling must NOT route
    try:
        CreateConnector(f"daos://{POOL}/{CONT}", loop, lcb, build_config(
            f"daos://{POOL}/{CONT}"), metadata)
        print("UNEXPECTED: daos:// url was routed")
        return 1
    except ValueError as e:
        print(f"expected rejection of daos:// url -> {e}")

    # 2) the real thing: routed via LMCache's factory, then a data roundtrip
    conn = CreateConnector(url, loop, lcb, config, metadata)
    print(f"routed {url} -> {type(conn).__name__}")

    shape, dtype = torch.Size([2, 4, 8]), torch.bfloat16
    mo = lcb.allocate(shape, dtype, MemoryFormat.KV_2LTD)
    assert mo is not None, "allocate returned None"
    view = mo.byte_array
    if isinstance(view, memoryview) and view.format == "<B":
        view = view.cast("B")
    nbytes = len(view)
    pattern = bytes((i * 11 + 5) & 0xFF for i in range(nbytes))
    view[:nbytes] = pattern

    key = CacheEngineKey("test-model", 1, 0, 0xFEEDFACE, dtype)
    try:
        loop.run_until_complete(conn.put(key, mo))
        print("put OK")
        assert loop.run_until_complete(conn.exists(key)), "exists False after put"
        got = loop.run_until_complete(conn.get(key))
        assert got is not None, "get returned None"
        gview = got.byte_array
        if isinstance(gview, memoryview) and gview.format == "<B":
            gview = gview.cast("B")
        assert bytes(gview[:nbytes]) == pattern, "payload mismatch"
        print("get + compare OK")

        # list()/remove_sync() through the same InstrumentedRemoteConnector
        # wrapper LMCache uses in production, not just via direct construction.
        from lmcache_daos.connector import _key_to_path

        names = set(loop.run_until_complete(conn.list()))
        assert _key_to_path(key).lstrip("/") in names, "list() missed the object"
        assert conn.remove_sync(key), "remove_sync returned False for a live key"
        assert not loop.run_until_complete(conn.exists(key)), \
            "object still present after remove_sync"
        print(f"list() via wrapper saw {len(names)} objects; remove_sync OK")
    finally:
        loop.run_until_complete(conn.close())
        loop.close()

    print("PASS plugin routing (CreateConnector -> DaosConnector -> DAOS DFS)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
