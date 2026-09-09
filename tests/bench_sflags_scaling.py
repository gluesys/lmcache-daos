# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""dfs_sys mount flags vs bulk-read thread scaling — merge decision for NO_LOCK.

Two lines of work fixed the same safety problem differently:

  * `main` (e90e389) turned locking ON (`sflags=0`) because the connector at that
    point **shared one handle across a 16-thread pool**, and `DFS_SYS_NO_LOCK`
    is documented as "useful for single-threaded applications". Its measurement
    (16 threads, 64 KiB objects) found the lock free: write 3397 vs 3090 obj/s,
    exists 23888 vs 24191 obj/s.
  * this branch kept `NO_LOCK` but gave **every worker thread its own handle**,
    so no handle is ever touched concurrently.

Both are safe. The open question is throughput in the regime `main`'s test did
not cover: **28 MiB objects, bulk read bandwidth**, where all the 34-35 GB/s
figures come from. A lock that is free at 64 KiB may not be free when 16 threads
stream 28 MiB each.

Caching matters too, and separately: `main` argues cache must stay on because
every path is "/<sha256>", so the root directory entry is looked up on every
operation. That only shows up if `open` is inside the measurement, so both are
measured:

  preopen  objects opened before the timer -> isolates bulk read bandwidth
  open+rd  open per read, as the connector's _get_sync actually does

Handle modes:
  perthread  one dfs_sys handle per thread   (this branch)
  shared     one handle for all threads      (main's assumption)

`shared` + NO_LOCK at 16 threads is the configuration `main` removed as unsafe.
It is measured for completeness -- if it is also slower, the decision is easy.

    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16 \
        python3 /lmd/tests/bench_sflags_scaling.py
"""

import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, "/lmd")

from lmcache_daos.dfs_binding import (DFS_SYS_NO_CACHE, DFS_SYS_NO_LOCK,
                                      DfsSys)

POOL = os.environ["DAOS_TEST_POOL"]
CONT = os.environ["DAOS_TEST_CONT"]
SZ = int(os.environ.get("SZ", 29360128))          # 28 MiB — the real chunk size
N = int(os.environ.get("N", 32))
THREADS = [int(x) for x in os.environ.get("THREADS", "1,4,16").split(",")]
REPS = int(os.environ.get("REPS", 2))

FLAGSETS = [
    ("NO_CACHE|NO_LOCK", DFS_SYS_NO_CACHE | DFS_SYS_NO_LOCK),  # this branch
    ("0 (cache+lock on)", 0),                                   # main
    ("NO_CACHE (lock on)", DFS_SYS_NO_CACHE),                   # isolates cache
]

setup = DfsSys(pool=POOL, cont=CONT, sflags=0)
try:
    src = ctypes.create_string_buffer(bytes([0x3C]) * SZ, SZ)
    for i in range(N):
        o = setup.open_rdwr_create(f"/sf_{i}")
        try:
            setup.write_obj_from(o, 0, SZ, src)
        finally:
            setup.close_obj(o)
    print(f"corpus {N} x {SZ>>20} MiB = {N*SZ/1e9:.2f} GB, best of {REPS}\n",
          flush=True)

    def run(sflags, shared, nthreads, preopen):
        """Return GB/s. Setup (connect / open / buffer touch) is untimed."""
        shard = [[i for i in range(N) if i % nthreads == k]
                 for k in range(nthreads)]
        errs = []
        barrier = threading.Barrier(nthreads + 1)
        shared_h = DfsSys(pool=POOL, cont=CONT, sflags=sflags) if shared else None
        holders = []

        def worker(k):
            d = None
            try:
                d = shared_h if shared else DfsSys(pool=POOL, cont=CONT,
                                                   sflags=sflags)
                if not shared:
                    holders.append(d)
                buf = ctypes.create_string_buffer(SZ)
                buf[0] = 0
                buf[SZ - 1] = 0
                pre = ({i: d.open_rdonly(f"/sf_{i}") for i in shard[k]}
                       if preopen else None)
                barrier.wait()                    # ---- timing starts ----
                for i in shard[k]:
                    if preopen:
                        d.read_obj_into(pre[i], 0, SZ, buf)
                    else:
                        obj = d.open_rdonly(f"/sf_{i}")
                        try:
                            d.read_obj_into(obj, 0, SZ, buf)
                        finally:
                            d.close_obj(obj)
                if pre:
                    for o in pre.values():
                        d.close_obj(o)
            except Exception as e:
                errs.append(f"{type(e).__name__}: {e}")
                try:
                    barrier.wait()
                except Exception:
                    pass
            finally:
                if d is not None and not shared:
                    try:
                        d.close()
                    except Exception:
                        pass

        th = [threading.Thread(target=worker, args=(k,)) for k in range(nthreads)]
        for x in th:
            x.start()
        barrier.wait()
        t0 = time.perf_counter()
        for x in th:
            x.join(timeout=600)
        dt = time.perf_counter() - t0
        if shared_h is not None:
            try:
                shared_h.close()
            except Exception:
                pass
        return (N * SZ / dt / 1e9), errs

    for preopen, label in ((True, "preopen (bulk read only)"),
                           (False, "open+read (as _get_sync does)")):
        print(f"=== {label} ===", flush=True)
        hdr = "".join(f"{t:>10}" for t in THREADS)
        print(f"{'sflags':<20}{'handles':<11}{hdr}", flush=True)
        for name, fl in FLAGSETS:
            for shared in (False, True):
                cells, note = [], ""
                for t in THREADS:
                    best, errs = 0.0, []
                    for _ in range(REPS):
                        g, e = run(fl, shared, t, preopen)
                        best = max(best, g)
                        errs += e
                    cells.append(f"{best:>10.2f}")
                    if errs and not note:
                        note = f"  ERR {errs[0][:38]}"
                mode = "shared" if shared else "perthread"
                unsafe = " *" if (shared and (fl & DFS_SYS_NO_LOCK)) else ""
                print(f"{name:<20}{mode+unsafe:<11}{''.join(cells)}{note}",
                      flush=True)
        print(flush=True)
    print("* = shared handle without locking: the configuration main removed as "
          "unsafe. Listed for comparison only.", flush=True)
finally:
    for i in range(N):
        try:
            setup.remove(f"/sf_{i}")
        except Exception:
            pass
    setup.close()
