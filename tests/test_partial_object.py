# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Partial / torn KV objects must never be served as a hit (T4).

A writer that dies mid-store leaves a short file behind. Because `exists()` is
just an open(), such a file looks present -- so `get()` is the only thing
standing between a torn object and corrupted KV being fed to the model.

This test fabricates the three interesting truncation points and asserts that
`get()` reports a miss (None) rather than returning a MemoryObj or letting an
exception escape into the serving path.

    DAOS_TEST_POOL=nvme_pool DAOS_TEST_CONT=lmcache_nvme \
        python tests/test_partial_object.py

Set VERBOSE=1 to print what each case actually did (used to characterise the
pre-fix behaviour).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POOL = os.environ.get("DAOS_TEST_POOL", "nvme_pool")
CONT = os.environ.get("DAOS_TEST_CONT", "lmcache_nvme")
VERBOSE = os.environ.get("VERBOSE") == "1"


def main():
    import torch
    from lmcache.v1.config import LMCacheEngineConfig
    from lmcache.v1.metadata import LMCacheMetadata
    from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend
    from lmcache.v1.memory_management import MemoryFormat
    from lmcache.v1.protocol import RemoteMetadata
    from lmcache.utils import CacheEngineKey
    from lmcache_daos import serde
    from lmcache_daos.connector import DaosConnector, _key_to_path

    config = LMCacheEngineConfig.from_defaults()
    metadata = LMCacheMetadata(
        model_name="test-model", world_size=1, local_world_size=1,
        worker_id=0, local_worker_id=0, kv_dtype=torch.bfloat16,
        kv_shape=(2, 256, 8, 1, 8),
    )
    lcb = LocalCPUBackend(config, metadata)

    shape, dtype = torch.Size([2, 4, 8]), torch.bfloat16
    mo = lcb.allocate(shape, dtype, MemoryFormat.KV_2LTD)
    assert mo is not None
    view = mo.byte_array
    if isinstance(view, memoryview) and view.format == "<B":
        view = view.cast("B")
    nbytes = len(view)
    view[:nbytes] = bytes((i * 13 + 7) & 0xFF for i in range(nbytes))

    loop = asyncio.new_event_loop()
    # Build the connector first: RemoteConnector.__init__ is what initialises the
    # module-level REMOTE_METADATA_FMT that RemoteMetadata.serialize() asserts on.
    conn = DaosConnector(f"daos://{POOL}/{CONT}", loop, lcb)

    kv_bytes = bytes(mo.byte_array)
    meta_bytes = RemoteMetadata(
        len(kv_bytes), mo.get_shapes(), mo.get_dtypes(),
        mo.get_memory_format()).serialize()
    blob = serde.pack(meta_bytes, kv_bytes)
    pfx = serde.prefix_size()

    cases = [
        ("empty file",           0),
        ("mid-prefix",           pfx // 2),
        ("prefix only",          pfx),
        ("mid-metadata",         pfx + max(1, len(meta_bytes) // 2)),
        ("metadata, no payload", pfx + len(meta_bytes)),
        ("mid-payload",          pfx + len(meta_bytes) + len(kv_bytes) // 2),
    ]

    failures = []
    try:
        for i, (name, cut) in enumerate(cases):
            key = CacheEngineKey("test-model", 1, 0, 0x70A70000 + i, dtype)
            path = _key_to_path(key)
            conn._dfs.remove(path)
            conn._dfs.write(path, blob[:cut])          # a torn store

            outcome = None
            try:
                got = loop.run_until_complete(conn.get(key))
                outcome = "None (miss)" if got is None else \
                          f"MemoryObj({len(bytes(got.byte_array))} B)"
            except Exception as e:
                outcome = f"raised {type(e).__name__}: {e}"

            ok = outcome == "None (miss)"
            print(f"  [{'ok ' if ok else 'BAD'}] truncated at {cut:5d} B "
                  f"({name:20s}) -> {outcome}")
            if not ok:
                failures.append((name, cut, outcome))
            conn._dfs.remove(path)
    finally:
        loop.run_until_complete(conn.close())
        loop.close()

    if failures:
        print(f"\nFAIL {len(failures)}/{len(cases)}: torn objects were not "
              f"reported as a clean miss")
        for name, cut, outcome in failures:
            print(f"  - {name} (cut {cut}): {outcome}")
        return 1
    print(f"\nPASS partial-object handling ({len(cases)} truncation points all "
          f"reported as a miss)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
