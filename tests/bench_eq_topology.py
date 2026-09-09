# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""How many event queues, how many pollers each? Find the eqx_lock sweet spot.

Source evidence (DAOS 2.8 src/client/api/event.c):

  * eq_progress_cb() runs tse_sched_progress(&eqx->eqx_sched) and then takes
    D_MUTEX_LOCK(&eqx->eqx_lock) to harvest the completion list; the event
    launch/complete paths take the same mutex. So **every submit and every
    completion on one EQ serialises on one mutex**.
  * the synchronous path has no EQ at all: ev_thpriv is initialised with
    DAOS_HDL_INVAL, so evx_ctx = daos_eq_ctx (the process-global context) and
    nothing touches an eqx_lock. That is why 16 blocking threads reach 35 GB/s.

So the async path is squeezed between two opposing costs:

    few EQs   -> eqx_lock contention                (measured: 1 EQ caps ~12.5)
    many EQs  -> one network context each, expensive (measured: 16 EQs -> 2.74)

This sweeps the 2-D space to find whether any (EQ count x pollers/EQ) point
closes the gap to the blocking path, or whether the async path is bounded below
it no matter how it is arranged.

    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16 python3 /lmd/tests/bench_eq_topology.py
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
SZ = int(os.environ.get("SZ", 29360128))
N = int(os.environ.get("N", 32))
WINDOW = int(os.environ.get("WINDOW", 16))     # total in-flight across all EQs
REPS = int(os.environ.get("REPS", 3))
GRID = os.environ.get("GRID", "1x1,1x4,2x2,2x4,4x1,4x2,4x4,8x2")

setup = DfsSys(pool=POOL, cont=CONT)


class Group:
    """One EQ plus the slots/buffers/pending-table that belong to it."""

    def __init__(self, nslots):
        self.eq = EventQueue()
        self.lk = threading.Lock()
        self.pending = {}
        self.free = []
        for _ in range(nslots):
            b = ctypes.create_string_buffer(SZ)
            b[0] = 0
            b[SZ - 1] = 0
            self.free.append((self.eq.new_event(canary=True), b))

    def close(self):
        try:
            self.eq.close(force=True)
        except Exception:
            pass


def run(neq, per_eq):
    nthreads = neq * per_eq
    slots_per_eq = max(1, WINDOW // neq)
    groups = [Group(slots_per_eq) for _ in range(neq)]
    handles = []
    objsets = []
    try:
        for _ in range(nthreads):
            d = DfsSys(pool=POOL, cont=CONT)
            handles.append(d)
            objsets.append({i: d.open_rdonly(f"/eqt_{i}") for i in range(N)})

        nxt = [0]
        glk = threading.Lock()
        errs = []
        barrier = threading.Barrier(nthreads + 1)

        def worker(tid):
            g = groups[tid % neq]
            d, objs = handles[tid], objsets[tid]
            try:
                barrier.wait()
                while True:
                    with glk:
                        remaining = nxt[0] < N
                    with g.lk:
                        idle = not g.pending
                    if not remaining and idle:
                        return
                    take = None
                    with g.lk:
                        if g.free:
                            slot_buf = g.free.pop()
                            take = slot_buf
                    if take is not None:
                        with glk:
                            idx = nxt[0] if nxt[0] < N else None
                            if idx is not None:
                                nxt[0] += 1
                        if idx is None:
                            with g.lk:
                                g.free.append(take)
                        else:
                            slot, buf = take
                            op = AsyncRead(idx, slot, objs[idx], buf, SZ)
                            op.submit(d)
                            with g.lk:
                                g.pending[op.addr] = op
                    for a in g.eq.poll(max_events=slots_per_eq, wait=True,
                                       timeout_us=2000):
                        with g.lk:
                            op = g.pending.pop(a, None)
                        if op is None:
                            continue
                        try:
                            op.check()
                        except Exception as e:
                            errs.append(f"{type(e).__name__}: {e}")
                        g.eq.reinit_event(op.slot)
                        with g.lk:
                            g.free.append((op.slot, op.dest))
            except Exception as e:
                errs.append(f"worker {type(e).__name__}: {e}")
                try:
                    barrier.wait()
                except Exception:
                    pass

        th = [threading.Thread(target=worker, args=(k,))
              for k in range(nthreads)]
        for x in th:
            x.start()
        barrier.wait()
        t0 = time.perf_counter()
        for x in th:
            x.join(timeout=180)
        dt = time.perf_counter() - t0
        return N * SZ / dt / 1e9, errs
    finally:
        for g in groups:
            g.close()
        for d in handles:
            try:
                d.close()
            except Exception:
                pass


try:
    for i in range(N):
        setup.write(f"/eqt_{i}", bytes([(i * 47 + 19) & 0xFF]) * SZ)
    print(f"corpus {N} x {SZ>>20}MB = {N*SZ/1e9:.2f} GB, "
          f"total in-flight {WINDOW}, best of {REPS}\n", flush=True)
    print(f"{'EQs x pollers':>14} {'threads':>8} {'GB/s':>8}   note", flush=True)
    for spec in GRID.split(","):
        neq, per = (int(x) for x in spec.strip().split("x"))
        best, allerr = 0.0, []
        for _ in range(REPS):
            g, e = run(neq, per)
            best = max(best, g)
            allerr += e
        note = f"{len(allerr)} err: {allerr[0][:50]}" if allerr else ""
        print(f"{spec:>14} {neq*per:>8} {best:>8.2f}   {note}", flush=True)
    print("\n기준: 동기 blocking 16스레드 = 35.3 GB/s (EQ 없음, eqx_lock 없음)",
          flush=True)
finally:
    for i in range(N):
        try:
            setup.remove(f"/eqt_{i}")
        except Exception:
            pass
    setup.close()
