"""Concurrent writers on the same key must never yield a corrupt hit (T5).

Two things are exercised:

1. **Same key, many writers.** Several threads store the same key at once. The
   connector's write path is open(O_CREAT) + write-at-0 + close, so writers
   overlap on one DFS object. Every reader must then see either a miss or the
   correct, complete payload -- never a blend of two writers' bytes.

2. **Reader racing a writer.** A reader hammers the key while writers churn it.
   This is the window that the plan document flags as "부분 저장된 chunk를 다른
   노드가 hit으로 판단하는 문제".

Each writer stores a *distinct* byte pattern of the same length. In real
LMCache use the key is a hash of the token prefix, so competing writers normally
carry identical KV -- but that also makes interleaving undetectable by
construction, so this test deliberately gives every writer its own pattern. A
reader must then see one of three things: a miss, or exactly one writer's
complete payload. Anything else is a blend of two writers, i.e. real tearing.

NOTE on loop affinity: the connector captures the loop handed to its ctor and
dispatches blocking libdfs work with ``that_loop.run_in_executor``. Driving it
from a *different* loop (e.g. one per thread) raises "got Future attached to a
different loop". LMCache always uses a single loop, so concurrency here is
modelled the same way: one loop running in its own thread, with many put/get
coroutines submitted via ``asyncio.run_coroutine_threadsafe``.

    DAOS_TEST_POOL=nvme_pool DAOS_TEST_CONT=lmcache_nvme \
        python tests/test_concurrent_writers.py
"""

import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POOL = os.environ.get("DAOS_TEST_POOL", "nvme_pool")
CONT = os.environ.get("DAOS_TEST_CONT", "lmcache_nvme")
WRITERS = int(os.environ.get("WRITERS", "6"))
ROUNDS = int(os.environ.get("ROUNDS", "40"))
PAYLOAD_KB = int(os.environ.get("PAYLOAD_KB", "128"))


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

    # A chunk-sized object so writes are big enough to actually interleave.
    elems = (PAYLOAD_KB * 1024) // 2 // 8 // 2   # bfloat16, shape [2, N, 8]
    shape, dtype = torch.Size([2, elems, 8]), torch.bfloat16

    def fresh_obj(seed):
        mo = lcb.allocate(shape, dtype, MemoryFormat.KV_2LTD)
        assert mo is not None, "allocate returned None"
        v = mo.byte_array
        if isinstance(v, memoryview) and v.format == "<B":
            v = v.cast("B")
        n = len(v)
        v[:n] = bytes((i * 31 + seed) & 0xFF for i in range(n))
        return mo, bytes(v[:n])

    # One distinct payload per writer, all the same length.
    patterns = []
    for w in range(WRITERS):
        _mo, pat = fresh_obj(17 * w + 3)
        patterns.append(pat)
    nbytes = len(patterns[0])
    valid = set(patterns)
    assert len(valid) == WRITERS, "writer payloads must be distinguishable"
    print(f"payload {nbytes} B, {WRITERS} distinct writers x {ROUNDS} rounds")

    bad = []
    lock = threading.Lock()

    def record(msg):
        with lock:
            bad.append(msg)

    # Run the connector's own loop in a thread; submit work to it from many
    # threads, mirroring how LMCache drives a single loop concurrently.
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    def submit(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop)

    reads = [0]

    for rnd in range(ROUNDS):
        key = CacheEngineKey("test-model", 1, 0, 0xC0DE0000 + rnd, dtype)
        path = _key_to_path(key)
        conn._dfs.remove(path)

        stop = threading.Event()

        def writer(i):
            try:
                mo, _ = fresh_obj(17 * i + 3)
                submit(conn.put(key, mo)).result(timeout=60)
            except Exception as e:
                record(f"round {rnd} writer raised {type(e).__name__}: {e}")

        def reader():
            while not stop.is_set():
                try:
                    got = submit(conn.get(key)).result(timeout=60)
                except Exception as e:
                    record(f"round {rnd} reader raised {type(e).__name__}: {e}")
                    return
                reads[0] += 1
                if got is None:
                    continue                      # miss is fine
                g = got.byte_array
                if isinstance(g, memoryview) and g.format == "<B":
                    g = g.cast("B")
                seen = bytes(g[:nbytes])
                if seen not in valid:
                    record(f"round {rnd} reader saw a BLENDED payload "
                           f"(matches no single writer)")
                    return

        rd = threading.Thread(target=reader, daemon=True)
        rd.start()
        ws = [threading.Thread(target=writer, args=(i,)) for i in range(WRITERS)]
        for w in ws:
            w.start()
        for w in ws:
            w.join()
        stop.set()
        rd.join(timeout=30)

        # After the dust settles the object must be complete and correct.
        final = submit(conn.get(key)).result(timeout=60)
        if final is None:
            record(f"round {rnd} final read was a MISS after {WRITERS} writers")
        else:
            f = final.byte_array
            if isinstance(f, memoryview) and f.format == "<B":
                f = f.cast("B")
            if bytes(f[:nbytes]) not in valid:
                record(f"round {rnd} final payload is a blend of writers")
        conn._dfs.remove(path)

    submit(conn.close()).result(timeout=60)
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=10)
    loop.close()

    if bad:
        print(f"\nFAIL {len(bad)} problem(s) over {reads[0]} racing reads:")
        for m in bad[:15]:
            print("  -", m)
        return 1
    print(f"\nPASS concurrent writers ({ROUNDS} rounds x {WRITERS} writers, "
          f"{reads[0]} racing reads saw only miss or one writer\'s complete "
          f"payload, final object never blended)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
