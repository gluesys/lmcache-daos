#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""The IO pool notices when it is wedged, and says so instead of queuing.

Background in doc/FAILURE-MODES.md: a DAOS call whose engine has died does not
return -- measured at 16 minutes, still spinning -- and a thread inside a C
call cannot be cancelled from Python. So the pool can reach a state it never
leaves, and everything submitted afterwards waits forever behind it.

Three properties, and the third is the one that makes this worth having rather
than a hazard of its own:

  1. a saturated pool with no completions is reported, promptly, as a raise
  2. ping() reports it too, so the health check cannot call a dead data path
     healthy on the strength of its own separate thread
  3. a saturated pool that is COMPLETING is not reported -- a large store keeps
     every worker busy for minutes quite legitimately, and a detector that
     cannot tell load from a wedge would take the backend down under exactly
     the load it exists to serve

No DAOS here: the mechanism is a lock, two counters and a clock, and the fault
it looks for is a thread that does not return, which a plain Event models
exactly. tests/failure_modes.py is where it meets a real dead server.

    python3 tests/test_wedge.py
"""
import asyncio
import concurrent.futures
import errno
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmcache_daos import connector as C           # noqa: E402
from lmcache_daos.connector import DaosConnector, DaosPoolWedged  # noqa: E402

fails = 0


def ck(what, ok, extra=""):
    global fails
    if not ok:
        fails += 1
    print("  %-56s %s %s" % (what, "PASS" if ok else "FAIL", extra))


def bare(workers=2):
    """A connector with only the wedge machinery wired up.

    __new__ rather than __init__ on purpose: __init__ connects to DAOS, and the
    point of this file is that the detector is testable without one. What it
    costs is that the field list here has to track __init__ -- if a future
    field is added and not set here, this raises AttributeError loudly rather
    than passing vacuously, which is the failure mode worth having.
    """
    c = DaosConnector.__new__(DaosConnector)
    c._io_lock = threading.Lock()
    c._workers = workers
    c._inflight = 0
    c._last_done = time.monotonic()
    c._pool = concurrent.futures.ThreadPoolExecutor(workers, thread_name_prefix="daos-io")
    c._ping_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="daos-ping")
    c._ping_inflight = False
    c.loop = asyncio.new_event_loop()
    return c


def main():
    C.STALL_SECS = 0.5          # 60 s by default; the logic is the same

    c = bare(workers=2)
    stuck = threading.Event()   # never set: this is the call that never returns
    try:
        wedge_detected(c, stuck)
    finally:
        # Release the workers however the assertions went. A failing test that
        # also leaves a process unable to exit reports itself as a hang, and a
        # hang is the thing under test -- the report would be unreadable.
        stuck.set()

    load_is_not_a_wedge()

    print("\n  === %s (%d failure%s) ===" % ("ALL PASS" if not fails else "FAILED",
                                             fails, "" if fails == 1 else "s"))
    return 1 if fails else 0


def wedge_detected(c, stuck):
    # Occupy every worker with a call that will not return.
    for _ in range(2):
        c._run(stuck.wait)
    time.sleep(0.05)            # let both actually reach a worker
    ck("both workers are occupied", c._inflight == 2, "(inflight=%d)" % c._inflight)
    ck("not reported as wedged before the stall window", c._wedged_for() == 0.0)

    time.sleep(C.STALL_SECS + 0.2)
    ck("reported as wedged once nothing completes", c._wedged_for() > 0)

    raised = None
    try:
        c._run(stuck.wait)
    except DaosPoolWedged as e:
        raised = e
    ck("a further submission raises DaosPoolWedged", raised is not None,
       "" if raised is None else "(%s)" % raised)
    ck("rc is EBUSY, so existing e.rc paths keep working",
       raised is not None and raised.rc == errno.EBUSY)
    ck("it is a DaosError, so per-key handlers still catch it",
       isinstance(raised, C.DaosError))

    # ping() must report the wedge and must not reach DAOS to do it: the probe
    # has its own thread, so it could otherwise answer a cheerful 0 from a pool
    # where every data worker has been stuck for minutes. (_dfs is unset here,
    # so anything that did reach DAOS would raise AttributeError and show up.)
    rc = c.loop.run_until_complete(c.ping())
    ck("ping() reports the wedge, not its own healthy thread",
       rc == errno.EBUSY, "(rc=%s)" % rc)

    t0 = time.perf_counter()
    c.loop.run_until_complete(c.close())
    dt = time.perf_counter() - t0
    ck("close() returns without joining the stuck threads", dt < 1.0, "(%.2fs)" % dt)


def load_is_not_a_wedge():
    """The false-positive case, and the reason this is not just a timer.

    Every worker stays occupied well past the stall window, but work keeps
    completing -- which is what a large store looks like. A detector that
    could not tell that from a wedge would disable the backend under exactly
    the load it exists to serve.
    """
    c = bare(workers=2)
    worst = 0.0

    async def churn():
        nonlocal worst
        end = time.monotonic() + C.STALL_SECS * 4
        while time.monotonic() < end:
            await asyncio.gather(*(c._run(time.sleep, 0.01) for _ in range(4)))
            worst = max(worst, c._wedged_for())

    c.loop.run_until_complete(churn())
    ck("a saturated pool that keeps completing is never called wedged",
       worst == 0.0, "(worst=%.2f)" % worst)
    c.loop.run_until_complete(c.close())


if __name__ == "__main__":
    sys.exit(main())
