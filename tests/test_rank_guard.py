#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Per-rank breaker, zombie pool and deadline engine, without DAOS.

The fake event queue behaves the way DAOS was measured to behave on a dead
rank (daos-reboot-repro/2026-10-13-kvt): abort does not complete the event,
fini of an uncompleted event fails (DER_BUSY), and the event completes only
when the rank comes back. A test that finalises or drops a zombie early
therefore fails here instead of segfaulting later.

    python3 tests/test_rank_guard.py
"""
import ctypes
import gc
import os
import random
import sys
import threading
import time
import weakref

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmcache_daos import rank_guard as G     # noqa: E402

fails = 0


def ck(what, ok, extra=""):
    global fails
    if not ok:
        fails += 1
    print("  %-62s %s %s" % (what, "PASS" if ok else "FAIL", extra))


class Keep:
    """Stands in for the iod/sgl/buffer bundle; weakref-able."""


class FakeSlot:
    _next = 0x1000

    def __init__(self):
        FakeSlot._next += 0x10
        self.addr = FakeSlot._next
        self.rc = None
        self.completed = False
        self.aborted = False
        self.finied = 0

    def error(self):
        return self.rc


class FakeEQ:
    def __init__(self):
        self.cv = threading.Condition()
        self.ready = []
        self.fini_errors = 0
        self.pollers = set()

    def new_event(self):
        return FakeSlot()

    def complete(self, slot, rc=0):
        with self.cv:
            slot.completed = True
            slot.rc = rc
            self.ready.append(slot.addr)
            self.cv.notify_all()

    def poll(self, max_events=16, wait=True, timeout_us=1000):
        self.pollers.add(threading.get_ident())
        with self.cv:
            if wait and not self.ready:
                self.cv.wait(timeout_us / 1e6)
            out, self.ready = self.ready[:max_events], self.ready[max_events:]
            return out

    def abort_event(self, slot):
        slot.aborted = True                   # and nothing else, like DAOS

    def fini_event(self, slot):
        if not slot.completed:
            self.fini_errors += 1
            raise RuntimeError("daos_event_fini: DER_BUSY")
        slot.finied += 1


class Cluster:
    """Ranks that answer after ``delay`` or, when down, hold every request."""

    def __init__(self, eq, delay=0.005):
        self.eq = eq
        self.delay = delay
        self.down = set()
        self.held = {}
        self.slots = []

    def submit(self, rank, rc=0):
        def go(slot):
            self.slots.append(slot)
            if rank in self.down:
                self.held.setdefault(rank, []).append(slot)
            else:
                threading.Timer(self.delay, self.eq.complete, (slot, rc)).start()
            return 0
        return go

    def revive(self, rank):
        self.down.discard(rank)
        for s in self.held.pop(rank, []):
            self.eq.complete(s, 0)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def wait_until(cond, secs=2.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def setup(**kw):
    eq = FakeEQ()
    clock = Clock()
    br = G.Breaker(clock=clock, **kw)
    eng = G.DeadlineEngine(eq, br, poll_timeout_us=2000)
    return eq, clock, br, eng, Cluster(eq)


def live_rank_completes():
    eq, _, br, eng, cl = setup()
    rc = eng.run(0, cl.submit(0), Keep(), 64, 0.5)
    ck("live rank: op completes with its rc", rc == 0, "(rc=%r)" % rc)
    ck("live rank: no zombie, slot finalised once",
       eng.zombies() == 0 and cl.slots[0].finied == 1)
    eng.close()


def dead_rank_parks_a_zombie():
    eq, _, br, eng, cl = setup(threshold=5)
    cl.down.add(1)
    k = Keep()
    ref = weakref.ref(k)
    t0 = time.monotonic()
    rc = eng.run(1, cl.submit(1), k, 4096, 0.05)
    dt = time.monotonic() - t0
    del k
    gc.collect()
    ck("dead rank: returns DEADLINE at the deadline", rc == G.DEADLINE and dt < 0.2,
       "(rc=%r %.3fs)" % (rc, dt))
    ck("dead rank: event aborted, never finalised", cl.slots[0].aborted
       and cl.slots[0].finied == 0 and eq.fini_errors == 0)
    ck("dead rank: buffers stay alive while the op is a zombie", ref() is not None)
    ck("dead rank: zombie counted against the rank",
       br.snapshot()[1]["zombies"] == 1 and br.snapshot()[1]["zombie_bytes"] == 4096)
    cl.revive(1)
    ok = wait_until(lambda: eng.zombies() == 0)
    gc.collect()
    ck("rank back: zombie reaped and finalised exactly once",
       ok and cl.slots[0].finied == 1 and eq.fini_errors == 0)
    ck("rank back: buffers released only after the reap", ref() is None)
    ck("rank back: zombie accounting returns to zero",
       br.snapshot()[1]["zombies"] == 0 and br.snapshot()[1]["zombie_bytes"] == 0)
    eng.close()


def breaker_opens_and_short_circuits():
    eq, _, br, eng, cl = setup(threshold=2, open_secs=5.0)
    cl.down.add(2)
    r1 = eng.run(2, cl.submit(2), Keep(), 1, 0.03)
    r2 = eng.run(2, cl.submit(2), Keep(), 1, 0.03)
    ck("two misses open the rank", r1 == r2 == G.DEADLINE and br.state(2) == G.OPEN)
    t0 = time.monotonic()
    skipped = [eng.run(2, cl.submit(2), Keep(), 1, 0.03) for _ in range(100)]
    dt = time.monotonic() - t0
    ck("open rank: requests skipped without being sent",
       all(r == G.SKIPPED for r in skipped) and len(cl.slots) == 2,
       "(%d sent)" % len(cl.slots))
    ck("open rank: skipping is immediate", dt < 0.05, "(%.4fs for 100)" % dt)
    ck("open rank: zombies stop growing", eng.zombies() == 2)
    rc = eng.run(0, cl.submit(0), Keep(), 1, 0.5)
    ck("other ranks are unaffected while one is open",
       rc == 0 and br.state(0) == G.CLOSED)
    cl.revive(2)
    eng.close()


def half_open_probe_and_backoff():
    eq, clock, br, eng, cl = setup(threshold=1, open_secs=5.0, max_open_secs=12.0)
    cl.down.add(3)
    eng.run(3, cl.submit(3), Keep(), 1, 0.03)
    ck("opened after one miss", br.state(3) == G.OPEN)
    clock.t += 4.9
    ck("still open before open_secs", eng.run(3, cl.submit(3), Keep(), 1, 0.03) == G.SKIPPED)
    clock.t += 0.2
    rc = eng.run(3, cl.submit(3), Keep(), 1, 0.03)
    ck("after open_secs one probe is sent", rc == G.DEADLINE and len(cl.slots) == 2)
    ck("failed probe reopens", br.state(3) == G.OPEN)
    clock.t += 9.9
    ck("backoff doubled: still open at 9.9 s", eng.run(3, cl.submit(3), Keep(), 1, 0.03)
       == G.SKIPPED)
    clock.t += 0.2
    eng.run(3, cl.submit(3), Keep(), 1, 0.03)
    clock.t += 11.9
    ck("backoff capped at max_open_secs", eng.run(3, cl.submit(3), Keep(), 1, 0.03)
       == G.SKIPPED)
    clock.t += 0.2
    # the rank comes back while open: its zombies complete and cut the wait short
    cl.revive(3)
    wait_until(lambda: eng.zombies() == 0)
    ck("returning rank: zombies reaped", eng.zombies() == 0 and eq.fini_errors == 0)
    rc = eng.run(3, cl.submit(3), Keep(), 1, 0.5)
    ck("returning rank: next request probes and succeeds", rc == 0)
    ck("returning rank: breaker closed, backoff reset",
       br.state(3) == G.CLOSED and br._st(3).backoff == 5.0)
    eng.close()


def zombie_cap_blocks_even_when_closed():
    eq, _, br, eng, cl = setup(threshold=100, zombie_cap=3)
    cl.down.add(4)
    rcs = [eng.run(4, cl.submit(4), Keep(), 1, 0.02) for _ in range(5)]
    ck("cap: misses below threshold keep the rank closed", br.state(4) == G.CLOSED)
    ck("cap: requests past zombie_cap are not sent",
       rcs == [G.DEADLINE] * 3 + [G.SKIPPED] * 2 and eng.zombies() == 3, "(%r)" % rcs)
    cl.revive(4)
    wait_until(lambda: eng.zombies() == 0)
    ck("cap: lifted once the zombies are reaped",
       eng.run(4, cl.submit(4), Keep(), 1, 0.5) == 0)
    eng.close()


def completion_at_the_deadline_is_handled_once():
    """A completion racing the abort must not be finalised twice or lost."""
    eq, _, br, eng, cl = setup(threshold=10 ** 6, zombie_cap=10 ** 6)

    rnd = random.Random(7)

    def jittered(slot):
        cl.slots.append(slot)
        # completes just before or just after the 1 ms deadline
        threading.Timer(rnd.uniform(0.0, 0.002), eq.complete, (slot, 0)).start()
        return 0

    rcs = [eng.run(5, jittered, Keep(), 1, 0.001) for _ in range(400)]
    wait_until(lambda: eng.zombies() == 0)
    once = all(s.finied == 1 for s in cl.slots)
    ck("race: both outcomes occurred", rcs.count(0) > 0 and rcs.count(G.DEADLINE) > 0,
       "(ok=%d deadline=%d)" % (rcs.count(0), rcs.count(G.DEADLINE)))
    ck("race: every slot finalised exactly once", once and eq.fini_errors == 0)
    ck("race: no zombie left behind", eng.zombies() == 0)
    eng.close()


def unreachable_rc_counts_as_a_miss():
    eq, _, br, eng, cl = setup(threshold=1)
    rc = eng.run(6, cl.submit(6, rc=G.DER_TIMEDOUT), Keep(), 1, 0.5)
    ck("DER_TIMEDOUT completion opens the rank", rc == G.DER_TIMEDOUT
       and br.state(6) == G.OPEN)
    rc = eng.run(7, cl.submit(7, rc=-1005), Keep(), 1, 0.5)     # DER_NONEXIST
    ck("other errors prove the rank answered", rc == -1005 and br.state(7) == G.CLOSED)
    eng.close()


def submit_failure_is_finalised_not_parked():
    eq, _, br, eng, cl = setup()

    def bad(slot):
        slot.completed = True                 # never launched: fini is legal
        return -1003
    rc = eng.run(0, bad, Keep(), 1, 0.5)
    ck("submit rc returned, nothing parked", rc == -1003 and eng.zombies() == 0
       and eq.fini_errors == 0)
    eng.close()


def per_rank_queues_one_poller():
    """Each rank on its own queue (own CaRT context), all polled by one thread."""
    q0, q1, qd = FakeEQ(), FakeEQ(), FakeEQ()
    br = G.Breaker(clock=Clock(), threshold=5)
    eng = G.DeadlineEngine(qd, br, rank_eqs={0: q0, 1: q1})
    c0, c1, cd = Cluster(q0), Cluster(q1), Cluster(qd)
    c1.down.add(1)
    rc1 = eng.run(1, c1.submit(1), Keep(), 1, 0.03)
    rc0 = [eng.run(0, c0.submit(0), Keep(), 1, 0.5) for _ in range(20)]
    rcd = eng.run(9, cd.submit(9), Keep(), 1, 0.5)
    ck("queues: each rank's events come from its own queue",
       len(c0.slots) == 20 and len(c1.slots) == 1 and len(cd.slots) == 1)
    ck("queues: live rank served while the other queue holds a zombie",
       rc1 == G.DEADLINE and rc0 == [0] * 20 and eng.zombies() == 1)
    ck("queues: a rank without its own queue uses the default", rcd == 0)
    c1.revive(1)
    ok = wait_until(lambda: eng.zombies() == 0)
    ck("queues: zombie reaped from its queue, finalised once",
       ok and c1.slots[0].finied == 1 and q1.fini_errors == 0)
    ps = q0.pollers | q1.pollers | qd.pollers
    ck("queues: one thread polls every queue", len(ps) == 1
       and q0.pollers == q1.pollers == qd.pollers, "(%d threads)" % len(ps))
    eng.close()


class SlowEQ(FakeEQ):
    """A queue whose poll costs ``cost`` seconds while ``slow`` is set, like
    a dead rank's CaRT context busy with resends."""

    def __init__(self, cost):
        super().__init__()
        self.cost, self.slow, self.polls = cost, False, 0

    def poll(self, max_events=16, wait=True, timeout_us=1000):
        self.polls += 1
        if self.slow:
            time.sleep(self.cost)
        return super().poll(max_events, wait, timeout_us)


def open_rank_queue_polled_slowly():
    q0, q1 = FakeEQ(), SlowEQ(0.02)
    clock = Clock()
    br = G.Breaker(clock=clock, threshold=1, open_secs=1000.0)
    eng = G.DeadlineEngine(FakeEQ(), br, rank_eqs={0: q0, 1: q1}, slow_poll_secs=0.2)
    c0, c1 = Cluster(q0, delay=0.0), Cluster(q1)
    c1.down.add(1)
    q1.slow = True
    eng.run(1, c1.submit(1), Keep(), 1, 0.05)
    ck("slow poll: rank opened", br.state(1) == G.OPEN)
    n0 = q1.polls
    t0 = time.monotonic()
    worst = 0.0
    while time.monotonic() - t0 < 1.0:
        s = time.monotonic()
        eng.run(0, c0.submit(0), Keep(), 1, 0.5)
        worst = max(worst, time.monotonic() - s)
    n = q1.polls - n0
    ck("slow poll: open rank's queue polled about every slow_poll_secs", 3 <= n <= 8,
       "(%d polls in 1 s)" % n)
    ck("slow poll: worst live op bounded by one slow poll, not one per round",
       worst < 0.06, "(%.3fs)" % worst)
    pm = eng.poll_max()
    ck("slow poll: poll_max shows the slow queue", pm.get(1, 0) >= 0.02, "(%r)" % pm)
    q1.slow = False
    c1.revive(1)
    ok = wait_until(lambda: eng.zombies() == 0)
    ck("slow poll: zombie still reaped", ok and c1.slots[0].finied == 1)
    eng.close()


def open_rank_poll_interval_adapts_to_cost():
    q0, q1 = FakeEQ(), SlowEQ(0.03)
    br = G.Breaker(clock=Clock(), threshold=1, open_secs=1000.0)
    eng = G.DeadlineEngine(FakeEQ(), br, rank_eqs={0: q0, 1: q1},
                           slow_poll_secs=0.01, slow_poll_factor=10.0)
    c0, c1 = Cluster(q0, delay=0.0), Cluster(q1)
    c1.down.add(1)
    q1.slow = True
    eng.run(1, c1.submit(1), Keep(), 1, 0.1)
    n0, t0 = q1.polls, time.monotonic()
    while time.monotonic() - t0 < 1.0:
        eng.run(0, c0.submit(0), Keep(), 1, 0.5)
    n = q1.polls - n0
    ck("adaptive: a 30 ms poll is repeated about every 300 ms, not every 10 ms",
       2 <= n <= 5, "(%d polls in 1 s)" % n)
    q1.slow = False
    c1.revive(1)
    ck("adaptive: zombie still reaped", wait_until(lambda: eng.zombies() == 0))
    eng.close()


def placement_is_stable_and_spread():
    calls = []

    def resolve(b):
        calls.append(b)
        return b % 3
    p = G.BucketPlacement(64, resolve)
    keys = [os.urandom(32) for _ in range(20000)]
    b1 = [p.bucket(k) for k in keys]
    ck("placement: same key, same bucket", b1 == [p.bucket(k) for k in keys])
    counts = [b1.count(i) for i in range(64)]
    ck("placement: every bucket used, none over 2x mean",
       min(counts) > 0 and max(counts) < 2 * len(keys) / 64, "(%d..%d)" % (min(counts),
                                                                            max(counts)))
    for b in b1[:500]:
        p.rank(b)
    ck("placement: resolve called once per bucket", len(calls) == len(set(calls)))


def layout_parse():
    """_first_rank reads daos/object.h's flexible arrays by offset."""
    shard = ctypes.create_string_buffer(4 + 8 * 2)
    ctypes.c_uint32.from_buffer(shard, 0).value = 2               # os_replica_nr
    ctypes.c_uint32.from_buffer(shard, 4).value = 7               # loc[0].sd_rank
    ctypes.c_uint32.from_buffer(shard, 8).value = 3               # loc[0].sd_tgt_idx
    lay = ctypes.create_string_buffer(16 + 8)
    ctypes.c_uint32.from_buffer(lay, 8).value = 1                 # ol_nr
    ctypes.c_void_p.from_buffer(lay, 16).value = ctypes.addressof(shard)
    r = G._first_rank(ctypes.c_void_p(ctypes.addressof(lay)))
    ck("layout: rank of shard 0 replica 0", r == 7, "(%r)" % r)


class FakeStore:
    """S1BucketStore's call shape over a DeadlineEngine, every key on ``rank``."""

    def __init__(self, eng, cl, rank, deadline=0.05):
        self.eng, self.cl, self.rank, self.deadline = eng, cl, rank, deadline
        self.error = None

    def fetch(self, dkey, items, single=False, on_reap=None):
        rc = self.eng.run(self.rank, self.cl.submit(self.rank), list(items), 64,
                          self.deadline, on_reap)
        return rc, ([n for _, _, n in items] if rc == 0 else None)

    def update(self, dkey, items, single=False):
        return self.eng.run(self.rank, self.cl.submit(self.rank), list(items), 64,
                            self.deadline)

    def punch(self, dkey, akeys=None):
        return self.eng.run(self.rank, self.cl.submit(self.rank), dkey, 0, self.deadline)


def guarded_obj_live_rank():
    eq, _, br, eng, cl = setup()
    g = G.GuardedObj(FakeStore(eng, cl, 0))
    got = g.fetch(b"k", [(b"m", None, 8), (b"p", None, 64)], single=True)
    ck("guarded: live fetch returns the byte counts", got == [8, 64], "(%r)" % got)
    try:
        g.update(b"k", [(b"p", None, 64)])
        g.punch(b"k")
        g.punch_akeys(b"k", [b"p"])
        ok = True
    except Exception as e:
        ok = False
        print("   ", e)
    ck("guarded: live update and punch do not raise", ok)
    eng.close()


def guarded_obj_dead_rank_fetch_is_a_miss():
    eq, _, br, eng, cl = setup(threshold=5)
    cl.down.add(1)
    g = G.GuardedObj(FakeStore(eng, cl, 1))
    got = g.fetch(b"k", [(b"m", None, 8)], single=True)
    ck("guarded: dead fetch without release reads as a miss", got == [0], "(%r)" % got)
    cl.revive(1)
    wait_until(lambda: eng.zombies() == 0)
    eng.close()


def guarded_obj_parks_the_destination():
    eq, _, br, eng, cl = setup(threshold=5)
    cl.down.add(1)
    g = G.GuardedObj(FakeStore(eng, cl, 1))
    released = []
    try:
        g.fetch(b"k", [(b"p", None, 64)], release=lambda: released.append(1))
        parked = False
    except G.Parked:
        parked = True
    ck("guarded: dead fetch with a release raises Parked", parked)
    ck("guarded: destination not released while DAOS may write it", released == [])
    cl.revive(1)
    ok = wait_until(lambda: released == [1])
    time.sleep(0.02)
    ck("guarded: released exactly once, at the reap", ok and released == [1],
       "(%r)" % released)
    eng.close()


def guarded_obj_skip_keeps_ownership():
    eq, _, br, eng, cl = setup(threshold=1, open_secs=60.0)
    cl.down.add(1)
    g = G.GuardedObj(FakeStore(eng, cl, 1))
    try:
        g.fetch(b"k", [(b"p", None, 64)])     # opens the breaker
    except G.Parked:
        pass
    released = []
    try:
        got = g.fetch(b"k", [(b"p", None, 64)], release=lambda: released.append(1))
    except G.Parked:
        got = "parked"
    ck("guarded: skipped fetch is a plain miss, not Parked", got == [0], "(%r)" % (got,))
    cl.revive(1)
    wait_until(lambda: eng.zombies() == 0)
    time.sleep(0.02)
    ck("guarded: skipped fetch's release never called by the guard", released == [])
    eng.close()


def guarded_obj_writes_raise_dropped():
    eq, _, br, eng, cl = setup(threshold=1, open_secs=60.0)
    cl.down.add(1)
    g = G.GuardedObj(FakeStore(eng, cl, 1))
    rcs = []
    for call in (lambda: g.update(b"k", [(b"p", None, 64)]),
                 lambda: g.update(b"k", [(b"m", None, 8)], single=True),
                 lambda: g.punch(b"k"), lambda: g.punch_akeys(b"k", [b"p"])):
        try:
            call()
            rcs.append(None)
        except G.Dropped as e:
            rcs.append(e.rc)
    ck("guarded: dead write is Dropped(DEADLINE), then SKIPPED",
       rcs == [G.DEADLINE, G.SKIPPED, G.SKIPPED, G.SKIPPED], "(%r)" % rcs)
    cl.revive(1)
    wait_until(lambda: eng.zombies() == 0)
    eng.close()


def guarded_obj_surfaces_connect_error():
    eq, _, br, eng, cl = setup()
    st = FakeStore(eng, cl, 0)
    st.error = RuntimeError("daos_pool_connect failed")
    g = G.GuardedObj(st)
    try:
        g.fetch(b"k", [(b"m", None, 8)])
        ok = False
    except RuntimeError:
        ok = True
    ck("guarded: a failed connect is raised, not served as misses", ok)
    eng.close()


def reap_callback_failure_keeps_poller():
    eq, _, br, eng, cl = setup(threshold=5)
    cl.down.add(1)

    def boom(rc):
        raise ValueError("bad callback")
    eng.run(1, cl.submit(1), Keep(), 64, 0.05, boom)
    cl.revive(1)
    wait_until(lambda: eng.zombies() == 0)
    rc = eng.run(0, cl.submit(0), Keep(), 64, 0.5)
    ck("on_reap: a raising callback does not stop the poller", rc == 0, "(rc=%r)" % rc)
    eng.close()


def main():
    for t in (live_rank_completes, dead_rank_parks_a_zombie,
              breaker_opens_and_short_circuits, half_open_probe_and_backoff,
              zombie_cap_blocks_even_when_closed,
              completion_at_the_deadline_is_handled_once,
              unreachable_rc_counts_as_a_miss, submit_failure_is_finalised_not_parked,
              per_rank_queues_one_poller, open_rank_queue_polled_slowly,
              open_rank_poll_interval_adapts_to_cost,
              placement_is_stable_and_spread, layout_parse,
              guarded_obj_live_rank, guarded_obj_dead_rank_fetch_is_a_miss,
              guarded_obj_parks_the_destination, guarded_obj_skip_keeps_ownership,
              guarded_obj_writes_raise_dropped, guarded_obj_surfaces_connect_error,
              reap_callback_failure_keeps_poller):
        print(t.__name__)
        t()
    print("FAIL" if fails else "OK", "(%d failed)" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
