"""Why is the event path slower? Isolate the confounds.

The earlier comparison (async ~7.8 vs sync 15.7 GB/s) changed THREE things at
once, so it could not attribute the gap:

  1. API path      dfs_sys_read (sync baseline)  vs  dfs_read (async)
  2. in-flight     16 pool threads               vs  window of 4
  3. completion    blocking call                 vs  event queue + poll

This benchmark varies one at a time, with everything expensive hoisted out of
the timed region (handles, event queues, object opens, destination buffers), and
the same corpus/byte count for every point.

Arms
  sysread(T)     T threads, blocking dfs_sys_read          <- production path
  dfsread(T)     T threads, blocking dfs_read (ev=NULL)    <- isolates confound 1
  async1(W)      1 poller, window W                        <- isolates 2 vs 3
  asyncP(P)      P pollers, window 1
  asyncPW(P,W)   P pollers, window W

Read "sysread(4) vs dfsread(4) vs async1(4)" as the clean three-way at equal
in-flight: any gap between the first two is the API path, any gap to the third is
the event machinery itself.

    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16 \
        python3 /lmd/tests/bench_async_vs_sync.py
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
SZ = int(os.environ.get("SZ", 29360128))     # 28 MiB
N = int(os.environ.get("N", 32))             # blobs read per measurement
REPS = int(os.environ.get("REPS", 3))        # take the best of REPS
TOTAL = N * SZ

setup = DfsSys(pool=POOL, cont=CONT)


class Worker(threading.Thread):
    """One thread: own handle, own pre-opened objects, own pooled buffers."""

    def __init__(self, indices, nslots, barrier, errs, mode, poll_max, poll_to):
        super().__init__()
        self.indices, self.nslots = indices, nslots
        self.barrier, self.errs = barrier, errs
        self.mode, self.poll_max, self.poll_to = mode, poll_max, poll_to

    def run(self):
        d = eq = None
        try:
            d = DfsSys(pool=POOL, cont=CONT)
            objs = {i: d.open_rdonly(f"/avs_{i}") for i in self.indices}
            bufs = [ctypes.create_string_buffer(SZ) for _ in range(self.nslots)]
            for b in bufs:                     # first-touch so page faults are
                b[0] = 0; b[SZ - 1] = 0        # not charged to the timed region
            if self.mode == "async":
                eq = EventQueue()
                slots = [eq.new_event(canary=True) for _ in range(self.nslots)]
            elif self.mode == "dfsread":
                d.base()                       # resolve dfs_t* before timing
            self.barrier.wait()                # ---- timing starts ----

            if self.mode == "sysread":
                for i in self.indices:
                    d.read_obj_into(objs[i], 0, SZ, bufs[0])
            elif self.mode == "dfsread":
                # Same call the async arm uses, but with a NULL event: strips
                # the event machinery while keeping the dfs_read code path.
                for i in self.indices:
                    self._sync_dfsread(d, objs[i], bufs[0])
            else:
                pending = {}
                free = list(zip(slots, bufs))
                nxt = 0
                while nxt < len(self.indices) or pending:
                    while nxt < len(self.indices) and free:
                        slot, buf = free.pop()
                        op = AsyncRead(self.indices[nxt], slot,
                                       objs[self.indices[nxt]], buf, SZ)
                        op.submit(d)
                        pending[op.addr] = (op, buf)
                        nxt += 1
                    # poll_to == 0 means "spin": NOWAIT, no blocking at all.
                    for a in eq.poll(max_events=self.poll_max,
                                     wait=self.poll_to != 0,
                                     timeout_us=self.poll_to):
                        got = pending.pop(a, None)
                        if got is None:
                            continue
                        op, buf = got
                        try:
                            op.check()
                        except Exception as e:
                            self.errs.append(f"{type(e).__name__}: {e}")
                        eq.reinit_event(op.slot)
                        free.append((op.slot, buf))
        except Exception as e:
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

    @staticmethod
    def _sync_dfsread(d, obj, buf):
        """dfs_read with ev=NULL -- caller-owned sgl, synchronous completion."""
        from lmcache_daos.dfs_binding import DIov, DSgList
        iov = DIov(ctypes.cast(buf, ctypes.c_void_p), SZ, SZ)
        sgl = DSgList(1, 1, ctypes.pointer(iov))
        size = ctypes.c_ulonglong(SZ)
        d.submit_read(obj, 0, sgl, size, 0)    # ev_addr = 0 -> NULL -> blocking


def measure(mode, workers, nslots, poll_max=None, poll_to=5000):
    best = 0.0
    errs_all = []
    for _ in range(REPS):
        shards = [[i for i in range(N) if i % workers == k]
                  for k in range(workers)]
        errs = []
        barrier = threading.Barrier(workers + 1)
        th = [Worker(shards[k], nslots, barrier, errs, mode,
                     poll_max or nslots, poll_to) for k in range(workers)]
        for x in th:
            x.start()
        barrier.wait()
        t0 = time.perf_counter()
        for x in th:
            x.join()
        dt = time.perf_counter() - t0
        best = max(best, TOTAL / dt / 1e9)
        errs_all += errs
    return best, errs_all


try:
    for i in range(N):
        setup.write(f"/avs_{i}", bytes([(i * 31 + 5) & 0xFF]) * SZ)
    print(f"corpus {N} x {SZ>>20}MB = {TOTAL/1e9:.2f} GB, best of {REPS}\n",
          flush=True)

    rows = []
    print("A. 동일 in-flight 3-way (API 경로 vs event 기계장치 분리)", flush=True)
    print(f"{'in-flight':>10} {'sysread':>9} {'dfsread':>9} {'async1':>9} "
          f"{'asyncP':>9}", flush=True)
    for L in (1, 2, 4, 8, 16):
        a, _ = measure("sysread", L, 1)
        b, _ = measure("dfsread", L, 1)
        c, e1 = measure("async", 1, L)          # 1 poller, window L
        dd, e2 = measure("async", L, 1)         # L pollers, window 1
        rows.append((L, a, b, c, dd))
        print(f"{L:>10} {a:>9.2f} {b:>9.2f} {c:>9.2f} {dd:>9.2f}"
              f"{'  ERR' if e1 or e2 else ''}", flush=True)

    print("\nB. async 조합 (폴러 x window)", flush=True)
    for p, w in ((2, 2), (2, 4), (4, 2), (4, 4), (8, 2)):
        g, e = measure("async", p, w)
        print(f"  pollers={p} window={w} inflight={p*w}: {g:.2f} GB/s"
              f"{'  ERR '+e[0] if e else ''}", flush=True)

    print("\nC. eq_poll 파라미터 (async1, window 8)", flush=True)
    for pm, to, lab in ((1, 5000, "max_events=1  timeout=5ms"),
                        (8, 5000, "max_events=8  timeout=5ms"),
                        (8, -1, "max_events=8  timeout=WAIT(-1)"),
                        (8, 0, "max_events=8  timeout=NOWAIT(spin)")):
        g, e = measure("async", 1, 8, poll_max=pm, poll_to=to)
        print(f"  {lab:<34}: {g:.2f} GB/s{'  ERR '+e[0] if e else ''}",
              flush=True)
finally:
    for i in range(N):
        try:
            setup.remove(f"/avs_{i}")
        except Exception:
            pass
    setup.close()
