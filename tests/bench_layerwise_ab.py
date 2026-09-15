#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""How much of the 46x survives the connector? DFS against dkey/akey.

doc/RAW-API-PLAN.md step 5, and its stopping condition. The case for moving to
dkey/akey is a per-object fixed cost of 0.0137 ms against DFS's 0.63 ms
(doc/LAYERWISE-MEASUREMENT.md) -- but that was measured at the raw API, with
none of the connector above it. This measures the same two paths through the
REAL connector: same put()/get(), same serde, same thread pool, same MemoryObj
allocation. Only the container differs, and the container is what picks the
backend.

Object size is swept because the whole argument is about fixed cost. At 16 MiB
the fixed part is a third of the time and the gap should nearly close; at 64 KiB
it is almost all of it and the gap should be close to the raw ratio. A result
that does not move with size would mean the fixed cost is not where the model
says it is.

Layerwise is the reason to care: splitting a chunk by layer multiplies the
object count by the layer count and shrinks each one, which is exactly the
regime where DFS lost (464 ms against 269 ms, needing 0.056 ms per object and
costing 0.696). Note that this measures the FIXED COST only -- the connector
still writes one payload akey per chunk, so the layer folding serde_v3 makes
possible is not in play here and its 2.4x is not included.

    DAOS_TEST_POOL=kvpool DFS_CONT=pingtest RAW_CONT=nixltest \\
        python3 tests/bench_layerwise_ab.py

Both containers must exist, the first POSIX and the second not.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POOL = os.environ.get("DAOS_TEST_POOL", "kvpool")
DFS_CONT = os.environ.get("DFS_CONT", "pingtest")
RAW_CONT = os.environ.get("RAW_CONT", "nixltest")
REPS = int(os.environ.get("REPS", "3"))
NOBJ = int(os.environ.get("NOBJ", "40"))          # one chunk's worth of layers
SIZES = [int(x) for x in os.environ.get(
    "SIZES", "65536,262144,1048576,4194304,16777216").split(",")]


def build(cont):
    import torch
    from lmcache.v1.config import LMCacheEngineConfig
    from lmcache.v1.metadata import LMCacheMetadata
    from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend
    from lmcache_daos.connector import DaosConnector

    cfg = LMCacheEngineConfig.from_defaults()
    cfg.remote_url = "daos://%s/%s" % (POOL, cont)
    cfg.local_cpu = False
    meta = LMCacheMetadata(model_name="bench-ab", world_size=1, local_world_size=1,
                           worker_id=0, local_worker_id=0, kv_dtype=torch.bfloat16,
                           kv_shape=(2, 256, 8, 1, 8))
    lcb = LocalCPUBackend(cfg, meta)
    loop = asyncio.new_event_loop()
    return DaosConnector("daos://%s/%s" % (POOL, cont), loop, lcb), loop, lcb


def run(conn, loop, lcb, nbytes, tag):
    """One store+load round for NOBJ objects of nbytes. Returns (put_ms, get_ms)."""
    import torch
    from lmcache.utils import CacheEngineKey
    from lmcache.v1.memory_management import MemoryFormat

    keys = [CacheEngineKey("bench-ab", 1, 0, 0xB000 + hash(tag) % 4096 + i,
                           torch.bfloat16) for i in range(NOBJ)]
    objs = []
    for i in range(NOBJ):
        mo = lcb.allocate(torch.Size([2, nbytes // 4]), torch.bfloat16,
                          MemoryFormat.KV_2LTD)
        if mo is None:
            raise RuntimeError("allocate returned None at %d B -- staging pool too small"
                               % nbytes)
        v = mo.byte_array
        v = v.cast("B") if isinstance(v, memoryview) else memoryview(v).cast("B")
        v[:len(v)] = bytes([(i * 31 + 7) & 0xFF]) * len(v)
        objs.append(mo)

    for k in keys:
        try:
            conn.remove_sync(k)
        except Exception:
            pass

    # The connector does NOT release the caller's reference -- that is
    # InstrumentedRemoteConnector's job, and nothing wraps us here. So the
    # harness plays the wrapper, or every object leaks and LMCache says so at
    # collection time.
    t0 = time.perf_counter()
    loop.run_until_complete(conn.batched_put(keys, objs))
    put_ms = (time.perf_counter() - t0) * 1e3
    for mo in objs:
        try:
            mo.ref_count_down()
        except Exception:
            pass

    t0 = time.perf_counter()
    got = loop.run_until_complete(conn.batched_get(keys))
    get_ms = (time.perf_counter() - t0) * 1e3

    hits = sum(1 for g in got if g is not None)
    if hits != NOBJ:
        raise RuntimeError("%s: %d/%d hits at %d B -- measuring a miss path"
                           % (tag, hits, NOBJ, nbytes))
    for g in got:
        try:
            g.ref_count_down()
        except Exception:
            pass
    for k in keys:
        try:
            conn.remove_sync(k)
        except Exception:
            pass
    return put_ms, get_ms


def main():
    print("pool=%s  dfs=%s  raw=%s  objects=%d  reps=%d"
          % (POOL, DFS_CONT, RAW_CONT, NOBJ, REPS))
    arms = {}
    for tag, cont in (("dfs", DFS_CONT), ("raw", RAW_CONT)):
        conn, loop, lcb = build(cont)
        if (tag == "raw") != bool(conn._raw):
            print("  %s: container %s picked the wrong backend -- check its layout"
                  % (tag, cont))
            return 2
        arms[tag] = (conn, loop, lcb)

    print("\n%-10s %10s %10s %10s %10s %10s" %
          ("object", "dfs put", "raw put", "dfs get", "raw get", "get 배수"))
    for nbytes in SIZES:
        best = {}
        for tag in ("dfs", "raw"):
            conn, loop, lcb = arms[tag]
            p = g = 1e30
            for _ in range(REPS):
                pm, gm = run(conn, loop, lcb, nbytes, tag)
                p, g = min(p, pm), min(g, gm)
            best[tag] = (p / NOBJ, g / NOBJ)     # per object
        ratio = best["dfs"][1] / best["raw"][1] if best["raw"][1] else 0
        label = ("%d KiB" % (nbytes >> 10)) if nbytes < (1 << 20) else ("%d MiB" % (nbytes >> 20))
        print("%-10s %9.3f%s %9.3f%s %9.3f%s %9.3f%s %9.2fx" % (
            label, best["dfs"][0], "", best["raw"][0], "",
            best["dfs"][1], "", best["raw"][1], "", ratio))

    for conn, loop, _ in arms.values():
        loop.run_until_complete(conn.close())
    print("\n  (숫자는 객체당 ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
