"""The configuration we never tried: ONE event queue, MANY poller threads.

Source reading (DAOS 2.8 src/client/api/event.c) reframes the problem:

  * synchronous ops (ev == NULL) use the thread-private event, initialised with
    DAOS_HDL_INVAL, so `evx_ctx = daos_eq_ctx` -- the single PROCESS-GLOBAL CaRT
    context (line 1071). Every thread calls crt_progress_cond() on that same
    context in daos_event_priv_wait(), and this scales to 35 GB/s at 16 threads.
  * an event on a user EQ gets `evx_ctx = eqx->eqx_ctx` (line 1062), and
    daos_eq_create() makes a NEW context per EQ (line 667). daos_eq_poll() then
    progresses only that one context.

So the sync path is "many threads driving one context" and our async path was
"one thread driving one context". Upstream also warns that each EQ creates a new
network context and that applications should limit how many they create -- which
explains why our earlier "more pollers" test (one EQ *per* poller) got worse
rather than better: it multiplied network contexts instead of progress threads.

This benchmark isolates the remaining variable: a single shared EQ progressed by
P threads. If throughput scales with P, the async design is viable after all and
the earlier conclusion ("event queues give completion notification but not
concurrency") was really "one poller cannot drive enough progress".

Caveat being tested deliberately: daos_eq_poll's thread safety is NOT documented
(upstream README makes no statement). Concurrent poll on one EQ is exactly what
we are probing, so a crash or a wrong completion here is itself the result.

    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16 \
        POLLERS=1,2,4,8,16 python3 /lmd/tests/bench_shared_eq.py
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
SZ = int(os.environ.get("SZ", 29360128))       # 28 MiB
N = int(os.environ.get("N", 32))
WINDOW = int(os.environ.get("WINDOW", 16))     # total in-flight, shared
POLLERS = [int(x) for x in os.environ.get("POLLERS", "1,2,4,8,16").split(",")]
REPS = int(os.environ.get("REPS", 3))

setup = DfsSys(pool=POOL, cont=CONT)


def run(nthreads):
    """One shared EQ, `nthreads` threads that both submit and poll it."""
    eq = EventQueue()
    handles, objsets, slots, bufs = [], [], [], []
    try:
        # ---- setup, all untimed ----------------------------------------
        for _ in range(nthreads):
            d = DfsSys(pool=POOL, cont=CONT)
            handles.append(d)
            objsets.append({i: d.open_rdonly(f"/seq_{i}") for i in range(N)})
        for _ in range(WINDOW):
            slots.append(eq.new_event(canary=True))
            b = ctypes.create_string_buffer(SZ)
            b[0] = 0
            b[SZ - 1] = 0
            bufs.append(b)

        free = list(zip(slots, bufs))       # guarded by `lk`
        pending = {}                        # guarded by `lk`
        lk = threading.Lock()
        nxt = [0]
        errs = []
        barrier = threading.Barrier(nthreads + 1)

        def worker(tid):
            d = handles[tid]
            objs = objsets[tid]
            try:
                barrier.wait()              # ---- timing starts ----
                while True:
                    with lk:
                        if nxt[0] >= N and not pending:
                            return
                        take = None
                        if nxt[0] < N and free:
                            take = (free.pop(), nxt[0])
                            nxt[0] += 1
                    if take is not None:
                        (slot, buf), idx = take
                        op = AsyncRead(idx, slot, objs[idx], buf, SZ)
                        op.submit(d)
                        with lk:
                            pending[op.addr] = op
                    # Concurrent poll on the SHARED eq -- the thing under test.
                    addrs = eq.poll(max_events=WINDOW, wait=True,
                                    timeout_us=2000)
                    for a in addrs:
                        with lk:
                            op = pending.pop(a, None)
                        if op is None:
                            # Another thread already retired it, or the address
                            # is not ours: that would mean poll handed the same
                            # completion to two threads.
                            errs.append(f"dup/unknown completion {a:#x}")
                            continue
                        try:
                            op.check()
                        except Exception as e:
                            errs.append(f"{type(e).__name__}: {e}")
                        eq.reinit_event(op.slot)
                        with lk:
                            free.append((op.slot, op.dest))
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
            x.join(timeout=120)
        dt = time.perf_counter() - t0
        return N * SZ / dt / 1e9, errs
    finally:
        try:
            eq.close(force=True)
        except Exception:
            pass
        for d in handles:
            try:
                d.close()
            except Exception:
                pass


try:
    for i in range(N):
        setup.write(f"/seq_{i}", bytes([(i * 43 + 17) & 0xFF]) * SZ)
    print(f"corpus {N} x {SZ>>20}MB = {N*SZ/1e9:.2f} GB, "
          f"shared EQ, in-flight window {WINDOW}, best of {REPS}\n", flush=True)
    print(f"{'pollers':>8} {'GB/s':>8}   note", flush=True)
    for p in POLLERS:
        best, allerr = 0.0, []
        for _ in range(REPS):
            g, e = run(p)
            best = max(best, g)
            allerr += e
        note = f"{len(allerr)} err: {allerr[0][:60]}" if allerr else ""
        print(f"{p:>8} {best:>8.2f}   {note}", flush=True)
    print("\n비교: 동기 스레드풀 16스레드 = 35.3 GB/s, "
          "EQ-per-poller 16 = 2.74 GB/s (bench_async_vs_sync)", flush=True)
finally:
    for i in range(N):
        try:
            setup.remove(f"/seq_{i}")
        except Exception:
            pass
    setup.close()
