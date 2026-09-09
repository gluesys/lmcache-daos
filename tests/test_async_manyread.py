# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""P1 gate: the async read engine + pending table, under the integrity bar.

This is test_manyread.py's 30 x 28 MB integrity check, re-run through the
event-queue path instead of the synchronous thread-pool path. It is the gate the
refactoring plan sets for P1, plus the two things the plan's lifetime analysis
implies but does not prove:

  * completions arrive **out of submission order** and are still matched to the
    right buffer (that is what the pending table is for);
  * nothing is dropped early -- every event is ``daos_event_fini``'d and the
    pending table drains to empty.

Pass criteria: 30/30 blobs byte-identical, pending table empty, 0 canary hits.

Run inside the serving container:
    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16 \
        python3 /lmd/tests/test_async_manyread.py
"""

import ctypes
import hashlib
import os
import sys
import time

sys.path.insert(0, "/lmd")

from lmcache_daos.daos_event import AsyncRead, EventQueue, DaosEventABIError
from lmcache_daos.dfs_binding import DfsSys

POOL = os.environ["DAOS_TEST_POOL"]
CONT = os.environ["DAOS_TEST_CONT"]
SZ = int(os.environ.get("SZ", 29360128))        # 28 MiB, same as test_manyread
N = int(os.environ.get("N", 30))
MAX_INFLIGHT = int(os.environ.get("MAX_INFLIGHT", 16))  # mirrors the pool size
VERIFY = os.environ.get("VERIFY", "1") == "1"

d = DfsSys(pool=POOL, cont=CONT)
eq = EventQueue()
fail = 0
vtime = 0.0           # time spent hashing, excluded from the throughput figure
pending = {}          # event address -> AsyncRead   (the pending table)
completion_order = []

try:
    # -- prepare identical corpus to test_manyread ------------------------
    digests = []
    for i in range(N):
        data = bytes([(i * 13 + 7) & 0xFF]) * SZ
        d.write(f"/amr_{i}", data)
        digests.append(hashlib.md5(data).hexdigest())
    print(f"wrote {N} x {SZ // (1 << 20)}MB", flush=True)

    def submit(i):
        """Open + allocate + submit one read, and register it as pending."""
        obj = d.open_rdonly(f"/amr_{i}")
        dest = ctypes.create_string_buffer(SZ)
        slot = eq.new_event(canary=True)
        # AsyncRead holds obj/dest/sgl/iov/size/slot; `pending` holds AsyncRead.
        # Note obj is deliberately NOT closed here -- DAOS still needs it.
        op = AsyncRead(i, slot, obj, dest, SZ)
        op.submit(d)
        pending[op.addr] = op
        return op

    def harvest(block=True):
        """Drain whatever has completed; verify and retire each."""
        global fail, vtime
        addrs = eq.poll(max_events=MAX_INFLIGHT, wait=block, timeout_us=5000)
        for a in addrs:
            op = pending.pop(a, None)
            if op is None:
                fail += 1
                print(f"  FAIL unknown completion {a:#x} -- event address is "
                      f"not a stable pending-table key", flush=True)
                continue
            completion_order.append(op.key)
            try:
                op.check()
                # md5 over 28 MB is ~50 ms; doing it inside the harvest loop
                # would otherwise land in the throughput number, so time it
                # separately and subtract.
                if VERIFY:
                    _v0 = time.perf_counter()
                    ok = (hashlib.md5(op.dest.raw[:SZ]).hexdigest()
                          == digests[op.key])
                    vtime += time.perf_counter() - _v0
                    if not ok:
                        fail += 1
                        print(f"  CORRUPT blob #{op.key}", flush=True)
            except DaosEventABIError as e:
                fail += 1
                print(f"  FAIL canary blob #{op.key}: {e}", flush=True)
            except Exception as e:
                fail += 1
                print(f"  FAIL blob #{op.key}: {type(e).__name__}: {e}",
                      flush=True)
            finally:
                # Lifecycle: fini only AFTER the consumer is done with the
                # buffer, then drop the last strong reference.
                eq.fini_event(op.slot)
                d.close_obj(op.obj)
        return len(addrs)

    # -- windowed submit / harvest ---------------------------------------
    t0 = time.perf_counter()
    nxt = 0
    while nxt < N or pending:
        while nxt < N and len(pending) < MAX_INFLIGHT:
            submit(nxt)
            nxt += 1
        harvest(block=True)
    wall = time.perf_counter() - t0

    total = N * SZ
    io = wall - vtime
    print(f"async read: {total/1e9:.2f} GB in {io:.2f}s "
          f"= {total/io/1e9:.2f} GB/s (inflight<={MAX_INFLIGHT}, "
          f"verify {vtime:.2f}s excluded)", flush=True)

    # -- lifecycle / ordering assertions ---------------------------------
    if pending:
        fail += 1
        print(f"  FAIL pending table not drained: {len(pending)} left "
              f"(leaked events/buffers)", flush=True)
    else:
        print("PASS pending table drained to empty", flush=True)

    if len(completion_order) != N:
        fail += 1
        print(f"  FAIL harvested {len(completion_order)} completions, "
              f"expected {N}", flush=True)

    if completion_order == sorted(completion_order):
        # Not a failure -- just means this run happened to complete in order, so
        # the out-of-order path went unexercised. Say so rather than imply it
        # was proven.
        print("NOTE completions arrived in submission order this run; "
              "out-of-order matching was not exercised", flush=True)
    else:
        print(f"PASS out-of-order completions matched correctly "
              f"(first 10: {completion_order[:10]})", flush=True)

    print(f"DONE test_async_manyread: {N - fail}/{N} OK, {fail} failure(s)",
          flush=True)
finally:
    # Teardown contract: abort anything still in flight BEFORE releasing
    # buffers, or DAOS writes into freed memory.
    for op in list(pending.values()):
        eq.abort_event(op.slot)
    for _ in range(200):
        if not pending:
            break
        try:
            addrs = eq.poll(max_events=16, wait=False)
        except Exception:
            break
        for a in addrs:
            op = pending.pop(a, None)
            if op is not None:
                eq.fini_event(op.slot)
                d.close_obj(op.obj)
    eq.close(force=True)
    for i in range(N):
        try:
            d.remove(f"/amr_{i}")
        except Exception:
            pass
    d.close()

sys.exit(1 if fail else 0)
