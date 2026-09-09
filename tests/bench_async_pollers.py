# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Does the event-queue path scale with poller threads?

Motivation: a single-poller async engine measured ~4.8 GB/s where the existing
synchronous thread-pool path does ~15.8 GB/s on the same box. The suspected
cause is that DAOS client progress is *caller-driven* -- the network engine only
advances while a thread is inside a DAOS call -- so one polling thread provides
one progress engine, while the sync path gets one per pool worker (each blocked
inside dfs_read with the GIL released).

If that is right, throughput climbs with the number of pollers, and the "async
lets us delete the thread pool" argument in the refactoring plan does not hold:
we would still need threads, just moved behind an event queue.

Everything expensive that is not I/O -- dfs_sys_connect, daos_eq_create, event
allocation, 28 MB destination buffers -- is set up **before** a barrier, so the
timed region contains only submit/poll/complete. (An earlier version timed the
per-thread connect and manufactured a fake "does not scale" curve.)

Each poller owns its own dfs_sys handle (DFS_SYS_NO_LOCK) and its own event
queue; daos_eq_poll's cross-thread guarantees are unspecified, so queues are
never shared.

    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16 \
        POLLERS=1,2,4,8 WINDOW=4 python3 /lmd/tests/bench_async_pollers.py
"""

import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, "/lmd")

from lmcache_daos.daos_event import AsyncRead, EventQueue
from lmcache_daos.dfs_binding import DfsSys

POOL = os.environ["DAOS_TEST_POOL"]
CONT = os.environ["DAOS_TEST_CONT"]
SZ = int(os.environ.get("SZ", 29360128))          # 28 MiB
N = int(os.environ.get("N", 32))                  # blobs per pass
WINDOW = int(os.environ.get("WINDOW", 4))         # in-flight per poller
POLLERS = [int(x) for x in os.environ.get("POLLERS", "1,2,4,8").split(",")]
PREOPEN = os.environ.get("PREOPEN", "0") == "1"

setup = DfsSys(pool=POOL, cont=CONT)


class Poller(threading.Thread):
    """Owns a handle, an event queue, and WINDOW reusable slots + buffers."""

    def __init__(self, indices, barrier, errs):
        super().__init__()
        self.indices = indices
        self.barrier = barrier
        self.errs = errs

    def run(self):
        d = eq = None
        try:
            # ---- setup (untimed) ----------------------------------------
            d = DfsSys(pool=POOL, cont=CONT)
            eq = EventQueue()
            slots = [eq.new_event(canary=True) for _ in range(WINDOW)]
            bufs = {id(s): ctypes.create_string_buffer(SZ) for s in slots}
            pending = {}
            free = list(slots)
            # dfs_sys_open is a synchronous metadata RPC. Leaving it in the
            # submit loop means every async read is preceded by a blocking
            # round trip, which would cap the pipeline regardless of how well
            # the reads themselves overlap. PREOPEN=1 hoists them out so the
            # timed region measures read overlap alone.
            pre = ({i: d.open_rdonly(f"/apb_{i}") for i in self.indices}
                   if PREOPEN else None)

            self.barrier.wait()          # ---- timing starts here --------

            nxt = 0
            while nxt < len(self.indices) or pending:
                while nxt < len(self.indices) and free:
                    i = self.indices[nxt]
                    obj = pre[i] if PREOPEN else d.open_rdonly(f"/apb_{i}")
                    slot = free.pop()
                    op = AsyncRead(i, slot, obj, bufs[id(slot)], SZ)
                    op.submit(d)
                    pending[op.addr] = op
                    nxt += 1
                for a in eq.poll(max_events=WINDOW, wait=True, timeout_us=5000):
                    op = pending.pop(a, None)
                    if op is None:
                        continue
                    try:
                        op.check()
                    except Exception as e:
                        self.errs.append(f"{type(e).__name__}: {e}")
                    if not PREOPEN:
                        d.close_obj(op.obj)
                    eq.reinit_event(op.slot)   # recycle, keep the allocation
                    free.append(op.slot)
            if pre:
                for o in pre.values():
                    d.close_obj(o)
        except Exception as e:                 # setup failures must not hang
            self.errs.append(f"setup {type(e).__name__}: {e}")
            try:
                self.barrier.wait()
            except Exception:
                pass
        finally:
            if eq is not None:
                eq.close(force=True)
            if d is not None:
                d.close()


try:
    for i in range(N):
        setup.write(f"/apb_{i}", bytes([(i * 29 + 3) & 0xFF]) * SZ)
    print(f"corpus: {N} x {SZ // (1 << 20)}MB = {N*SZ/1e9:.2f} GB, "
          f"window={WINDOW}/poller", flush=True)

    print(f"{'pollers':>8} {'inflight':>9} {'GB/s':>8}   note", flush=True)
    for t in POLLERS:
        shards = [[i for i in range(N) if i % t == k] for k in range(t)]
        errs = []
        barrier = threading.Barrier(t + 1)
        th = [Poller(shards[k], barrier, errs) for k in range(t)]
        for x in th:
            x.start()
        barrier.wait()
        t0 = time.perf_counter()
        for x in th:
            x.join()
        dt = time.perf_counter() - t0
        note = f"{len(errs)} error(s): {errs[0]}" if errs else ""
        print(f"{t:>8} {t*WINDOW:>9} {N*SZ/dt/1e9:>8.2f}   {note}", flush=True)

    print("\ncompare: bench_ceiling.py (sync thread-pool, pooled buffers)",
          flush=True)
finally:
    for i in range(N):
        try:
            setup.remove(f"/apb_{i}")
        except Exception:
            pass
    setup.close()
