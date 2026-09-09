# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Can we get completion-ordered delivery WITHOUT DAOS event queues?

diag_async_serialize established that DAOS event queues give asynchronous
*completion notification* but not concurrent *execution*: submit returns in
0.13 ms, yet per-op latency grows linearly with the in-flight window (5.6 ->
15.4 -> 59.1 ms for window 1 -> 4 -> 16) and aggregate throughput stays pinned
near 7 GB/s. Meanwhile the plain blocking path scales 15 -> 35 GB/s purely by
adding threads.

If that is right, the thing the upstream RFC actually needs -- chunks yielded in
completion order so the engine can start H2D on chunk i while i+1 is still in
flight -- is already available from the existing thread pool: each worker does a
blocking read and pushes its result the moment it lands. No event queue, no ABI
risk, no lifetime table.

This checks exactly that: N blocking readers, results consumed as they complete.
Pass criteria: full throughput (comparable to the blocking sweep), integrity
intact, and completions genuinely out of submission order.

    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16 \
        python3 /lmd/tests/diag_completion_order.py
"""

import ctypes
import hashlib
import os
import queue
import sys
import threading
import time

sys.path.insert(0, "/lmd")

from lmcache_daos.dfs_binding import DfsSys

POOL = os.environ["DAOS_TEST_POOL"]
CONT = os.environ["DAOS_TEST_CONT"]
SZ = int(os.environ.get("SZ", 29360128))     # 28 MiB
N = int(os.environ.get("N", 32))
WORKERS = int(os.environ.get("WORKERS", 16))
VERIFY = os.environ.get("VERIFY", "1") == "1"

setup = DfsSys(pool=POOL, cont=CONT)
try:
    digests = []
    for i in range(N):
        data = bytes([(i * 41 + 13) & 0xFF]) * SZ
        setup.write(f"/co_{i}", data)
        digests.append(hashlib.md5(data).hexdigest())
    print(f"corpus {N} x {SZ>>20}MB = {N*SZ/1e9:.2f} GB, {WORKERS} workers\n",
          flush=True)

    # Shard up front, round-robin. Draining a shared queue at startup lets the
    # first thread to reach it take everything, which silently turns this into a
    # single-threaded run (measured 7 GB/s and perfectly in-order delivery --
    # both artifacts, not results).
    shards = [[i for i in range(N) if i % WORKERS == k] for k in range(WORKERS)]
    done = queue.Queue()
    barrier = threading.Barrier(WORKERS + 1)
    errs = []

    def worker(mine):
        try:
            d = DfsSys(pool=POOL, cont=CONT)
            buf = ctypes.create_string_buffer(SZ)
            buf[0] = 0
            buf[SZ - 1] = 0
            objs = {i: d.open_rdonly(f"/co_{i}") for i in mine}
            barrier.wait()                      # ---- timing starts ----
            mv = memoryview(buf)
            for i in mine:
                d.read_obj_into(objs[i], 0, SZ, buf)
                # Yield the moment this chunk lands. In the real connector this
                # is where the consumer would kick off its H2D copy for slot i.
                #
                # NB: never touch `buf.raw` here -- ctypes .raw COPIES the whole
                # 28 MB under the GIL, which serialises every worker and drops
                # this from ~30 GB/s to ~6. Hash the memoryview instead (hashlib
                # releases the GIL for large inputs, so the hashes run in
                # parallel).
                done.put((i, hashlib.md5(mv[:SZ]).hexdigest() if VERIFY
                          else None))
            for o in objs.values():
                d.close_obj(o)
            d.close()
        except Exception as e:
            errs.append(f"{type(e).__name__}: {e}")
            try:
                barrier.wait()
            except Exception:
                pass

    th = [threading.Thread(target=worker, args=(shards[k],))
          for k in range(WORKERS)]
    for x in th:
        x.start()
    barrier.wait()
    t0 = time.perf_counter()

    order, bad, vtime = [], 0, 0.0
    for _ in range(N):
        i, payload = done.get()
        order.append(i)
        if VERIFY and payload != digests[i]:
            bad += 1
    for x in th:
        x.join()
    wall = time.perf_counter() - t0

    # The md5 runs inside the worker, so it is real work in the pipeline; report
    # both so the number is not mistaken for a pure read figure.
    print(f"throughput: {N*SZ/wall/1e9:.2f} GB/s "
          f"({'with' if VERIFY else 'without'} per-chunk md5 in the worker)",
          flush=True)
    print(f"integrity : {N-bad}/{N} OK", flush=True)
    inorder = order == sorted(order)
    print(f"delivery  : {'IN submission order (no reordering seen)' if inorder else 'COMPLETION order'}"
          f" -- first 12: {order[:12]}", flush=True)
    if errs:
        print(f"errors    : {errs[:2]}", flush=True)
    print(f"\n판정: completion-ordered 전달은 {'가능' if not inorder else '이번 런에서 미관측'}"
          f", event queue 불필요", flush=True)
finally:
    for i in range(N):
        try:
            setup.remove(f"/co_{i}")
        except Exception:
            pass
    setup.close()
