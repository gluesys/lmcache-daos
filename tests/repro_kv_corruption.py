# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Minimal reproducer for the KV corruption, without vLLM and without a GPU.

Why this exists. End-to-end, a cache hit returns wrong KV 30-85% of the time
depending on host, while every layer underneath measures clean:

    raw DFS I/O, 28 MiB x 16 threads, repeated   byte-exact, 80/80
    LMCache LocalCPUBackend (no DAOS)            correct and deterministic
    LMCache + DAOS remote backend                30-85% wrong

Changing connector code does not move it (main, branch-with-copy and
branch-with-alias all sit at 30-40% on one host) and changing host does (40% ->
85%). So the fault is not in a commit of this repository, and the remaining
suspects are the connector's interaction with LMCache's MemoryObj lifecycle and
LMCache itself. This strips vLLM, the GPU, the model and the generation
comparison out of the picture and drives only that interaction, so a failure
here is a reproducer someone else can run -- and a clean pass would move
suspicion back up into vLLM integration.

    DAOS_TEST_POOL=gdspool DAOS_TEST_CONT=kvlmc5 \\
        python3 tests/repro_kv_corruption.py [chunk_MiB] [keys] [rounds] [gets]

It mirrors production deliberately in three places, because each one is a
candidate:

  * batched_put / batched_get, not put / get. Those are the paths LMCache
    actually uses (support_batched_* return True) and the ones that fan out
    across the connector's 16-thread pool.
  * ref_count_up() before handing an object to batched_put, because that is
    what LMCache's NaiveSerializer does -- it returns the SAME object with one
    reference added for the consumer. Without emulating it the connector's own
    release would drop the count to zero early and the test would be measuring
    something production never does.
  * every key gets its own byte pattern, so a mismatch can say whether the data
    belongs to a DIFFERENT key rather than just "wrong".

Each round re-reads every key `gets` times. That distinguishes the two failure
shapes the end-to-end harness separates:
    all reads agree but differ from what was written -> stored object is wrong
    reads disagree with each other                   -> the read path is
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POOL = os.environ.get("DAOS_TEST_POOL", "gdspool")
CONT = os.environ.get("DAOS_TEST_CONT", "kvlmc5")


def pattern(seed: int, n: int) -> bytes:
    """Per-key, position-dependent bytes.

    Position dependence catches a shifted or partial read; the seed catches a
    read that returned another key's payload.
    """
    base = bytes(((i * 31 + seed * 101) & 0xFF) for i in range(4096))
    return (base * (n // len(base) + 1))[:n]


def as_bytes(mo) -> memoryview:
    v = mo.byte_array
    if not isinstance(v, memoryview):
        v = memoryview(v)
    return v.cast("B")


def main() -> int:
    chunk_mib = int(sys.argv[1]) if len(sys.argv) > 1 else 28
    n_keys = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    n_gets = int(sys.argv[4]) if len(sys.argv) > 4 else 3

    import torch
    from lmcache.utils import CacheEngineKey
    from lmcache.v1.config import LMCacheEngineConfig
    from lmcache.v1.memory_management import MemoryFormat
    from lmcache.v1.metadata import LMCacheMetadata
    from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend

    from lmcache_daos.connector import DaosConnector, _key_to_path

    # bf16, shape (2, N, 8) -> 32*N bytes. Pick N for the requested size.
    nbytes = chunk_mib * 1024 * 1024
    n_elem = nbytes // 32
    shape = torch.Size([2, n_elem, 8])
    dtype = torch.bfloat16

    config = LMCacheEngineConfig.from_defaults()
    # Room for every object plus the ones get() allocates on top.
    want_gb = max(4, (chunk_mib * n_keys * 4) // 1024 + 2)
    for attr in ("max_local_cpu_size", "local_cpu_size"):
        if hasattr(config, attr):
            try:
                setattr(config, attr, want_gb)
            except Exception:
                pass
    metadata = LMCacheMetadata(
        model_name="repro-model", world_size=1, local_world_size=1,
        worker_id=0, local_worker_id=0, kv_dtype=dtype,
        kv_shape=(2, 256, 8, 1, 8),
    )
    lcb = LocalCPUBackend(config, metadata)

    print(f"pool={POOL} cont={CONT} chunk={chunk_mib}MiB keys={n_keys} "
          f"rounds={rounds} gets_per_round={n_gets} cpu_budget={want_gb}GB")

    loop = asyncio.new_event_loop()
    conn = DaosConnector(f"daos://{POOL}/{CONT}", loop, lcb)

    keys = [CacheEngineKey("repro-model", 1, 0, 0x1000 + i, dtype)
            for i in range(n_keys)]
    wanted = {i: pattern(i, nbytes) for i in range(n_keys)}

    stats = {"pass": 0, "stored_wrong": 0, "reads_disagree": 0,
             "miss": 0, "short": 0}
    details: list[str] = []

    try:
        for r in range(rounds):
            # Start each round from a clean namespace so a stale object from a
            # previous round cannot be mistaken for a fresh failure.
            for k in keys:
                try:
                    conn._dfs.remove(_key_to_path(k))
                except Exception:
                    pass

            objs = []
            for i in range(n_keys):
                mo = lcb.allocate(shape, dtype, MemoryFormat.KV_2LTD)
                if mo is None:
                    print(f"  allocate returned None at key {i} -- CPU budget "
                          f"too small; lower keys/chunk_MiB")
                    return 2
                as_bytes(mo)[:nbytes] = wanted[i]
                # Emulate NaiveSerializer.serialize(): same object, +1 ref for
                # the consumer. The connector releases exactly this one.
                if hasattr(mo, "ref_count_up"):
                    mo.ref_count_up()
                objs.append(mo)

            try:
                loop.run_until_complete(conn.batched_put(keys, objs))
            finally:
                # Drop the reference allocate() gave US. The connector already
                # released the consumer reference the serializer emulation
                # added, so this returns each object to the pool. Without it
                # LMCache logs "garbage collected with ref_count=1" and the
                # pool never reclaims, which would change behaviour under load
                # and make this harness the thing being measured.
                for mo in objs:
                    if hasattr(mo, "ref_count_down"):
                        mo.ref_count_down()
                objs = []

            # Re-read the whole set n_gets times and compare every byte.
            reads: list[list[bytes | None]] = []
            for g in range(n_gets):
                got = loop.run_until_complete(conn.batched_get(keys))
                snap: list[bytes | None] = []
                for mo in got:
                    if mo is None:
                        snap.append(None)
                    else:
                        snap.append(bytes(as_bytes(mo)[:nbytes]))
                        if hasattr(mo, "ref_count_down"):
                            mo.ref_count_down()
                reads.append(snap)

            for i in range(n_keys):
                vals = [reads[g][i] for g in range(n_gets)]
                if any(v is None for v in vals):
                    stats["miss"] += 1
                    details.append(f"r{r} k{i}: MISS on "
                                   f"{sum(v is None for v in vals)}/{n_gets} reads")
                    continue
                if any(len(v) != nbytes for v in vals):
                    stats["short"] += 1
                    continue
                if all(v == wanted[i] for v in vals):
                    stats["pass"] += 1
                    continue
                # Which reads are wrong, and which are right. Reporting the
                # first read unconditionally is useless: the first observed
                # failure had reads[0] byte-perfect and a later read wrong, so
                # the diagnostic printed "first diff at -1, 0 pages differ".
                wrong = [g for g, v in enumerate(vals) if v != wanted[i]]
                if len(set(vals)) > 1:
                    stats["reads_disagree"] += 1
                    note = (f"reads disagree; read(s) {wrong} wrong, "
                            f"{[g for g in range(n_gets) if g not in wrong]} correct")
                else:
                    stats["stored_wrong"] += 1
                    note = "all reads agree, all differ from written"

                v = vals[wrong[0]] if wrong else vals[0]
                first = next((p for p in range(nbytes) if v[p] != wanted[i][p]), -1)
                owner = "unknown"
                for j in range(n_keys):
                    if v[:4096] == wanted[j][:4096]:
                        owner = f"key {j}'s pattern"
                        break
                bad = sum(1 for p in range(0, nbytes, 4096)
                          if v[p] != wanted[i][p])
                details.append(
                    f"r{r} k{i}: {note}; first diff at {first}, "
                    f"{bad}/{nbytes // 4096} sampled pages differ, "
                    f"head looks like {owner}")
    finally:
        loop.run_until_complete(conn.close())
        loop.close()

    total = sum(stats.values())
    print()
    for line in details[:20]:
        print("  " + line)
    if len(details) > 20:
        print(f"  ... {len(details) - 20} more")
    print()
    print(f"objects checked      {total}")
    print(f"  pass               {stats['pass']}")
    print(f"  stored wrong       {stats['stored_wrong']}")
    print(f"  reads disagree     {stats['reads_disagree']}")
    print(f"  miss               {stats['miss']}")
    print(f"  short read         {stats['short']}")
    bad = total - stats["pass"]
    if total:
        print(f"\nfailure rate {bad / total * 100:.1f}% ({bad}/{total})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
