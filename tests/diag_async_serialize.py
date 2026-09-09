# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Is the async read actually asynchronous? Time submit vs poll.

bench_async_vs_sync showed async throughput is FLAT (~7 GB/s) from in-flight 1
to 16 while the synchronous path scales 15 -> 35 GB/s. Flat throughput under
rising concurrency is the signature of serialization, so the question is where.

Two candidates, and they are distinguishable by timing:

  (a) dfs_read(ev) returns immediately and the reads genuinely overlap, but our
      poll loop fails to drive progress -> submit is fast, poll is slow, and
      per-op latency grows with the window.
  (b) dfs_read(ev) does the work INLINE and merely marks the event complete --
      i.e. the event does not actually make it asynchronous on this build ->
      submit itself takes roughly a whole read, and poll returns instantly.

If (b), the entire premise of the async refactor is unavailable in DAOS 2.8
regardless of how we write the Python side.

    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16 \
        python3 /lmd/tests/diag_async_serialize.py
"""

import ctypes
import os
import statistics as st
import sys
import time

sys.path.insert(0, "/lmd")

from lmcache_daos.daos_event import AsyncRead, EventQueue
from lmcache_daos.dfs_binding import DIov, DSgList, DfsSys

POOL = os.environ["DAOS_TEST_POOL"]
CONT = os.environ["DAOS_TEST_CONT"]
SZ = int(os.environ.get("SZ", 29360128))     # 28 MiB
N = int(os.environ.get("N", 16))

d = DfsSys(pool=POOL, cont=CONT)
try:
    for i in range(N):
        d.write(f"/dg_{i}", bytes([(i * 37 + 11) & 0xFF]) * SZ)
    objs = {i: d.open_rdonly(f"/dg_{i}") for i in range(N)}
    bufs = [ctypes.create_string_buffer(SZ) for _ in range(N)]
    for b in bufs:
        b[0] = 0
        b[SZ - 1] = 0
    d.base()
    print(f"{N} x {SZ>>20}MB, buffers pre-touched, objects pre-opened\n",
          flush=True)

    # ---- reference: one blocking dfs_read (ev = NULL) -------------------
    lat = []
    for i in range(N):
        iov = DIov(ctypes.cast(bufs[i], ctypes.c_void_p), SZ, SZ)
        sgl = DSgList(1, 1, ctypes.pointer(iov))
        size = ctypes.c_ulonglong(SZ)
        t0 = time.perf_counter()
        d.submit_read(objs[i], 0, sgl, size, 0)      # NULL event -> blocking
        lat.append((time.perf_counter() - t0) * 1000)
    print(f"sync dfs_read (ev=NULL): per-op {st.mean(lat):.2f} ms "
          f"= {SZ/(st.mean(lat)/1000)/1e9:.2f} GB/s", flush=True)

    # ---- the diagnostic: submit vs poll, at window 1 and window N -------
    for window in (1, 4, N):
        eq = EventQueue()
        slots = [eq.new_event(canary=True) for _ in range(window)]
        try:
            pending = {}
            free = list(zip(slots, range(window)))
            sub_ms, poll_ms, op_ms = [], [], []
            nxt = 0
            t_start = time.perf_counter()
            while nxt < N or pending:
                while nxt < N and free:
                    slot, bi = free.pop()
                    op = AsyncRead(nxt, slot, objs[nxt], bufs[bi], SZ)
                    t0 = time.perf_counter()
                    op.submit(d)                     # <-- (b) shows up here
                    sub_ms.append((time.perf_counter() - t0) * 1000)
                    pending[op.addr] = (op, bi, t0)
                    nxt += 1
                t0 = time.perf_counter()
                addrs = eq.poll(max_events=window, wait=True, timeout_us=5000)
                poll_ms.append((time.perf_counter() - t0) * 1000)
                for a in addrs:
                    got = pending.pop(a, None)
                    if got is None:
                        continue
                    op, bi, tsub = got
                    op_ms.append((time.perf_counter() - tsub) * 1000)
                    eq.reinit_event(op.slot)
                    free.append((op.slot, bi))
            wall = time.perf_counter() - t_start
            print(f"async window={window:<3} "
                  f"submit {st.mean(sub_ms):7.2f} ms/op | "
                  f"poll {sum(poll_ms):7.2f} ms total ({len(poll_ms)} calls) | "
                  f"op latency {st.mean(op_ms):7.2f} ms | "
                  f"{N*SZ/wall/1e9:.2f} GB/s", flush=True)
            frac = sum(sub_ms) / (sum(sub_ms) + sum(poll_ms)) * 100
            print(f"{'':>19}시간 배분: submit {frac:.0f}% / poll {100-frac:.0f}%"
                  f"  -> {'(b) dfs_read 가 인라인 수행' if frac > 60 else '(a) poll 이 지배'}",
                  flush=True)
        finally:
            eq.close(force=True)
finally:
    for i in range(N):
        try:
            d.remove(f"/dg_{i}")
        except Exception:
            pass
    d.close()
