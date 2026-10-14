# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Per-rank failure isolation for the raw object path (prototype).

Why this exists
---------------
On an rd_fac 0 pool with ``self_heal:none`` (the ``kvcache`` profile, hub
1f09478a), a powered-off node does not make DAOS fail anything. Measured on a
3-rank IB lane (daos-reboot-repro/2026-10-13-kvt, runs 1, 3a, 3b):

  * a request to a block on the dead rank never returns. The client resends
    it every 60 s for as long as the rank is down -- 1200 s and counting;
  * ``daos_event_abort`` hands control back but does not end the request.
    ``daos_event_fini`` says DER_BUSY, and none of 18 aborted events
    completed in 286 s of outage. Freeing their buffers anyway segfaulted the
    process at the next 60 s resend;
  * requests to the live ranks are unaffected throughout, including while the
    pool-service and management leaders fail over.

So the caller has to (1) bound every request itself, (2) keep what it aborted
alive until DAOS lets go of it, and (3) stop sending to that rank, because
every request it sends there becomes one more thing to keep alive for the
whole outage. That is a deadline, a zombie pool and a circuit breaker per
rank. The existing wedge detector in connector.py cannot do (3): it judges
the whole pool, so one dead rank of three eventually takes every worker and
then the live two thirds of the cache with it.

Placement
---------
A breaker per rank only helps if the rank is known *before* sending. ObjSys
puts every key in one object of the pool's default class, where the dkey hash
picks the shard inside DAOS and is not ours to compute. Here keys are spread
over ``nbuckets`` single-shard (S1) objects instead: our hash picks the
bucket, and ``daos_obj_layout_get`` -- client-side, computed from the pool
map, so it works during an outage -- says which rank holds it. A bucket never
moves, because this profile never excludes or reintegrates.

Pieces, each testable without DAOS (tests/test_rank_guard.py):

  * :class:`Breaker`      -- closed / open / half-open per rank
  * :class:`DeadlineEngine` -- one event queue, one poller thread, deadline on
    every op, aborted ops parked as zombies and reaped when DAOS completes them
  * :class:`BucketPlacement` -- key -> bucket -> rank
  * :class:`S1BucketStore` -- the above over real libdaos
  * :class:`GuardedObj`   -- ObjSys's interface over the store, which is how
    DaosConnector uses it (``DAOS_RANK_GUARD=1``)

Status: prototype. Measured on a real outage with tests/rank_guard_outage.py;
the connector wiring has unit tests only.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Results other than a DAOS rc. Negative DER_ codes are never this large.
DEADLINE = "DEADLINE"      # aborted at the caller's deadline, now a zombie
SKIPPED = "SKIPPED"        # breaker open: not sent at all
NOT_READY = "NOT_READY"    # store still connecting (outage at start-up)

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half-open"

# A completed op proves the rank answered -- any rc, a missing key included --
# except these, which DAOS returns when it could not reach the rank at all.
DER_TIMEDOUT, DER_OOG, DER_HG = -1011, -1019, -1020
UNREACHABLE = frozenset((DER_TIMEDOUT, DER_OOG, DER_HG))


# ---- breaker ----------------------------------------------------------------
class _RankState:
    __slots__ = ("state", "misses", "until", "backoff", "probe_out", "zombies",
                 "zombie_bytes")

    def __init__(self, backoff: float):
        self.state = CLOSED
        self.misses = 0
        self.until = 0.0
        self.backoff = backoff
        self.probe_out = False
        self.zombies = 0
        self.zombie_bytes = 0


class Breaker:
    """Circuit breaker per rank.

    ``threshold`` consecutive deadline misses open the rank for ``open_secs``,
    doubling up to ``max_open_secs`` while probes keep failing. When the time
    is up one real request goes through as the probe (half-open); success
    closes the rank, a miss opens it again.

    Zombies gate everything: a rank holding ``zombie_cap`` aborted requests
    gets nothing more, probes included, whatever its state. Every request
    sent to a dead rank is memory held for the rest of the outage, so the cap
    is what bounds that memory -- the timer alone does not, since each failed
    probe adds one.

    A zombie that completes means DAOS reached the rank again, so it cuts the
    current open period short and lets the next request probe.
    """

    def __init__(self, threshold: int = 2, open_secs: float = 5.0,
                 max_open_secs: float = 60.0, zombie_cap: int = 64,
                 clock: Callable[[], float] = time.monotonic):
        self.threshold = threshold
        self.open_secs = open_secs
        self.max_open_secs = max_open_secs
        self.zombie_cap = zombie_cap
        self._clock = clock
        self._lock = threading.Lock()
        self._ranks: Dict[int, _RankState] = {}

    def _st(self, rank: int) -> _RankState:
        st = self._ranks.get(rank)
        if st is None:
            st = self._ranks[rank] = _RankState(self.open_secs)
        return st

    def allow(self, rank: int) -> bool:
        with self._lock:
            st = self._st(rank)
            if st.zombies >= self.zombie_cap:
                return False
            if st.state == CLOSED:
                return True
            if st.state == OPEN and self._clock() >= st.until:
                st.state = HALF_OPEN
                st.probe_out = False
            if st.state == HALF_OPEN and not st.probe_out:
                st.probe_out = True
                return True
            return False

    def success(self, rank: int) -> None:
        with self._lock:
            st = self._st(rank)
            if st.state != CLOSED:
                logger.info("rank %d: breaker closed", rank)
            st.state = CLOSED
            st.misses = 0
            st.probe_out = False
            st.backoff = self.open_secs

    def miss(self, rank: int) -> None:
        with self._lock:
            st = self._st(rank)
            st.misses += 1
            if st.state == HALF_OPEN or st.misses >= self.threshold:
                st.state = OPEN
                st.until = self._clock() + st.backoff
                logger.warning("rank %d: breaker open for %.1fs (misses=%d zombies=%d)",
                               rank, st.backoff, st.misses, st.zombies)
                st.backoff = min(st.backoff * 2, self.max_open_secs)
                st.probe_out = False

    def zombie_added(self, rank: int, nbytes: int) -> None:
        with self._lock:
            st = self._st(rank)
            st.zombies += 1
            st.zombie_bytes += nbytes

    def zombie_done(self, rank: int, nbytes: int) -> None:
        with self._lock:
            st = self._st(rank)
            st.zombies -= 1
            st.zombie_bytes -= nbytes
            if st.state == OPEN:
                st.until = self._clock()      # the rank answered: probe now

    def state(self, rank: int) -> str:
        with self._lock:
            return self._st(rank).state

    def snapshot(self) -> Dict[int, dict]:
        with self._lock:
            return {r: {"state": s.state, "misses": s.misses, "zombies": s.zombies,
                        "zombie_bytes": s.zombie_bytes}
                    for r, s in sorted(self._ranks.items())}


# ---- deadline engine --------------------------------------------------------
class _Op:
    __slots__ = ("eq", "slot", "rank", "keep", "nbytes", "done", "rc", "t0", "on_reap")

    def __init__(self, eq, slot, rank, keep, nbytes, on_reap=None):
        self.eq = eq              # the queue the slot belongs to
        self.slot = slot
        self.rank = rank
        self.keep = keep          # every buffer DAOS may still touch
        self.nbytes = nbytes
        self.done = threading.Event()
        self.rc = None
        self.t0 = time.monotonic()
        self.on_reap = on_reap    # called with the rc if this becomes a zombie


class DeadlineEngine:
    """Run DAOS ops with a deadline on one event queue.

    ``eq`` is duck-typed on :class:`lmcache_daos.daos_event.EventQueue`:
    ``new_event()``, ``poll(max_events, wait, timeout_us)`` returning event
    addresses, ``abort_event(slot)``, ``fini_event(slot)``, and ``slot.addr`` /
    ``slot.error()``.

    One poller thread owns ``daos_eq_poll`` (the EventQueue contract). Callers
    submit from any thread and wait on their own op. At the deadline the
    caller aborts the event and moves the op -- slot, buffers and all -- into
    the zombie table. It is never finalised there: fini on an aborted event
    returns DER_BUSY, and dropping its buffers is the segfault seen in run 1.
    The poller reaps it when DAOS finally completes it.

    ``rank_eqs`` gives ranks their own queue; others use ``eq``. Each queue
    is its own CaRT context, and a dead rank's zombies -- resent by DAOS for
    the whole outage -- slowed every op sharing their context 20-100x on a
    real outage (lane A, 2026-10-14). The one poller thread still drives all
    of them, round robin without waiting: progress from several threads at
    once crashed inside libfabric with ofi+verbs (cxl2, 2026-10-14).

    One thread polling every queue is not enough on its own: a poll on the
    dead rank's queue itself takes tens of ms while DAOS works on the resends,
    and round robin hands that to every rank (lane A run C). So a queue whose
    rank's breaker is not closed is polled at most every ``slow_poll_secs``,
    and never more often than ``slow_poll_factor`` times its last poll's
    cost, so a dead context gets at most about 1/factor of the poller: that
    poll took 78 ms (median) while the node booted, and a fixed 50 ms still
    cut the live ranks to 514/s (run D). Its zombies only need reaping
    eventually, and its probes have a deadline far longer than that.
    """

    def __init__(self, eq, breaker: Breaker, poll_timeout_us: int = 10000,
                 rank_eqs: Optional[Dict[int, object]] = None,
                 slow_poll_secs: float = 0.05, slow_poll_factor: float = 10.0):
        self.eq = eq
        self.breaker = breaker
        self._poll_us = poll_timeout_us
        self._rank_eqs = dict(rank_eqs or {})
        self._eqs = [eq] + [q for q in self._rank_eqs.values() if q is not eq]
        self._eq_rank = {id(q): r for r, q in self._rank_eqs.items()}
        self._slow_poll = slow_poll_secs
        self._slow_factor = slow_poll_factor
        self._next_poll: Dict[int, float] = {}
        self._poll_max: Dict[object, float] = {}
        self._lock = threading.Lock()
        self._pending: Dict[int, _Op] = {}
        self._zombies: Dict[int, _Op] = {}
        self.stats = {"ok": 0, "error": 0, "deadline": 0, "skipped": 0,
                      "zombie_reaped": 0, "zombie_reaped_ok": 0}
        self._stop = False
        self._thread = threading.Thread(target=self._poller, name="daos-eq-poller",
                                        daemon=True)
        self._thread.start()

    # -- caller side ----------------------------------------------------------
    def run(self, rank: int, submit: Callable[[object], int], keep: object,
            nbytes: int, deadline: float,
            on_reap: Optional[Callable[[int], None]] = None):
        """Submit one op and wait at most ``deadline`` seconds.

        ``submit(slot)`` starts the DAOS call with that event and returns its
        immediate rc. ``keep`` must hold every buffer the call reads or
        writes. Returns the DAOS rc, :data:`DEADLINE` or :data:`SKIPPED`.

        ``on_reap(rc)`` runs on the poller thread if the op became a zombie
        and DAOS has now completed it -- for buffers ``keep`` cannot own,
        such as an allocator's, which must not go back before then.
        """
        if not self.breaker.allow(rank):
            self._count("skipped")
            return SKIPPED
        eq = self._rank_eqs.get(rank, self.eq)
        slot = eq.new_event()
        op = _Op(eq, slot, rank, keep, nbytes, on_reap)
        with self._lock:
            self._pending[slot.addr] = op
        rc = submit(slot)
        if rc != 0:
            # Not launched: nothing is in flight, so fini is safe.
            with self._lock:
                self._pending.pop(slot.addr, None)
            eq.fini_event(slot)
            self._count("error")
            return rc
        if not op.done.wait(deadline):
            with self._lock:
                late = op.done.is_set()       # completed while we took the lock
                if not late:
                    self._pending.pop(slot.addr, None)
                    eq.abort_event(slot)
                    self._zombies[slot.addr] = op
            if not late:
                self.breaker.zombie_added(rank, nbytes)
                self.breaker.miss(rank)
                self._count("deadline")
                return DEADLINE
        rc = op.rc
        eq.fini_event(slot)
        if rc in UNREACHABLE:
            self.breaker.miss(rank)
        else:
            self.breaker.success(rank)
        self._count("ok" if rc == 0 else "error")
        return rc

    # -- poller side ----------------------------------------------------------
    def _poller(self) -> None:
        if len(self._eqs) == 1:
            while not self._stop:
                self._poll_one(self.eq, True, self._poll_us)
            return
        idle = 0.00005
        while not self._stop:
            got = 0
            for q in self._eqs:
                r = self._eq_rank.get(id(q))
                slow = r is not None and self._slow_poll > 0 \
                    and self.breaker.state(r) != CLOSED
                t = time.monotonic()
                if slow and t < self._next_poll.get(id(q), 0.0):
                    continue
                got += self._poll_one(q, False, 0)
                dt = time.monotonic() - t
                key = "default" if r is None else r
                if dt > self._poll_max.get(key, 0.0):
                    self._poll_max[key] = dt
                if slow:
                    self._next_poll[id(q)] = t + max(self._slow_poll,
                                                     self._slow_factor * dt)
            if got:
                idle = 0.00005
            else:                             # back off to 1 ms while nothing moves
                time.sleep(idle)
                idle = min(idle * 2, 0.001)

    def _poll_one(self, q, wait: bool, timeout_us: int) -> int:
        try:
            addrs = q.poll(max_events=64, wait=wait, timeout_us=timeout_us)
        except Exception:                     # keep polling; one bad poll is not fatal
            logger.exception("daos_eq_poll failed")
            time.sleep(0.01)
            return 0
        for a in addrs:
            self._complete(a)
        return len(addrs)

    def _complete(self, addr: int) -> None:
        with self._lock:
            op = self._pending.pop(addr, None)
            if op is not None:
                op.rc = op.slot.error()
                op.done.set()
                return
            z = self._zombies.pop(addr, None)
        if z is None:
            logger.error("completion for unknown event 0x%x", addr)
            return
        rc = z.slot.error()
        z.eq.fini_event(z.slot)               # DAOS is done with it now
        self.breaker.zombie_done(z.rank, z.nbytes)
        self._count("zombie_reaped")
        if rc == 0:
            self._count("zombie_reaped_ok")
        logger.info("rank %d: zombie reaped rc=%d after %.1fs", z.rank, rc,
                    time.monotonic() - z.t0)
        if z.on_reap is not None:
            try:
                z.on_reap(rc)
            except Exception:                 # the poller must survive a bad callback
                logger.exception("zombie on_reap failed")

    # -- misc -----------------------------------------------------------------
    def _count(self, k: str) -> None:
        with self._lock:
            self.stats[k] += 1

    def poll_max(self) -> Dict[object, float]:
        """Longest single poll per queue since the last call, then reset."""
        m, self._poll_max = self._poll_max, {}
        return m

    def zombies(self) -> int:
        with self._lock:
            return len(self._zombies)

    def close(self) -> int:
        """Stop the poller. Returns the zombies left, which stay referenced:
        their buffers must outlive the process's last DAOS call."""
        self._stop = True
        self._thread.join(timeout=1.0)
        return self.zombies()


# ---- placement --------------------------------------------------------------
class BucketPlacement:
    """key -> bucket (our hash) -> rank (``resolve(bucket)``, cached)."""

    def __init__(self, nbuckets: int, resolve: Callable[[int], int]):
        if nbuckets <= 0:
            raise ValueError("nbuckets must be positive")
        self.nbuckets = nbuckets
        self._resolve = resolve
        self._rank: Dict[int, int] = {}

    def bucket(self, dkey: bytes) -> int:
        h = hashlib.blake2b(dkey, digest_size=8).digest()
        return int.from_bytes(h, "little") % self.nbuckets

    def rank(self, bucket: int) -> int:
        r = self._rank.get(bucket)
        if r is None:
            r = self._rank[bucket] = self._resolve(bucket)
        return r


# ---- real backend -----------------------------------------------------------
class _ShardLoc(ctypes.Structure):
    _fields_ = [("sd_rank", ctypes.c_uint32), ("sd_tgt_idx", ctypes.c_uint32)]


class _ObjShard(ctypes.Structure):      # daos/object.h: os_replica_nr + loc[0]
    _fields_ = [("os_replica_nr", ctypes.c_uint32)]


class _ObjLayout(ctypes.Structure):     # ol_ver, ol_class, ol_nr, *ol_shards[0]
    _fields_ = [("ol_ver", ctypes.c_uint32), ("ol_class", ctypes.c_uint32),
                ("ol_nr", ctypes.c_uint32)]


def _first_rank(layout_p) -> int:
    """Rank of shard 0, replica 0 of a ``struct daos_obj_layout *``.

    The flexible arrays are read by offset rather than declared: ol_shards
    follows three uint32 (pointer-aligned, so offset 16 on LP64), and
    os_shard_loc follows one uint32 in its shard (offset 4).
    """
    base = ctypes.cast(layout_p, ctypes.c_void_p).value
    lay = _ObjLayout.from_address(base)
    if lay.ol_nr < 1:
        raise RuntimeError("object layout has no shards")
    off = (ctypes.sizeof(_ObjLayout) + ctypes.alignment(ctypes.c_void_p) - 1) \
        // ctypes.alignment(ctypes.c_void_p) * ctypes.alignment(ctypes.c_void_p)
    shard0 = ctypes.c_void_p.from_address(base + off).value
    sh = _ObjShard.from_address(shard0)
    if sh.os_replica_nr < 1:
        raise RuntimeError("object shard has no replicas")
    return _ShardLoc.from_address(shard0 + ctypes.sizeof(_ObjShard)).sd_rank


def op_deadline(base: float, nbytes: int, min_bw: float) -> float:
    """``base`` plus the time ``nbytes`` takes at ``min_bw`` bytes/s.

    A fixed deadline cannot fit both a 28-byte header and a 7.3 MiB KV chunk:
    on lane A's zvol-backed targets the chunk writes of one batched put took
    0.5-0.8 s on healthy ranks, and a fixed 0.5 s opened the breaker on them
    15 times a minute (vLLM run H, 2026-10-14).
    """
    return base + (nbytes / min_bw if min_bw > 0 else 0.0)


class S1BucketStore:
    """Keys over ``nbuckets`` S1 objects, every op through a DeadlineEngine.

    Connecting is done on a daemon thread with the blocking calls, and the
    store answers :data:`NOT_READY` until it finishes. While any rank is
    down a new pool connect does not complete -- the PS cannot hand the
    handle to the dead rank (run 3a, DER_OOG) -- and it does complete, by
    itself, when the rank rejoins (154 s and 586 s measured). A thread parked
    in that call is the whole cost, and it turns a start-up during an outage
    into "no cache until the rank is back" instead of "no start-up".

    Deadlines (:func:`op_deadline`): a fetch gets ``deadline``, a write or
    punch ``write_deadline``, each plus its bytes at ``min_bw``. Reads are
    on the TTFT path; writes are not -- LMCache stores after the fact -- so
    they may wait long enough that only a rank that does not answer misses.
    """

    def __init__(self, pool: str, cont: str, nbuckets: int = 64,
                 oid_base: int = 1 << 40, deadline: float = 0.5,
                 breaker: Optional[Breaker] = None, sys: Optional[str] = None,
                 per_rank_eq: bool = True, slow_poll_secs: float = 0.05,
                 write_deadline: float = 10.0, min_bw: float = 50e6):
        self.pool, self.cont, self.sys = pool, cont, sys
        self.nbuckets = nbuckets
        self.oid_base = oid_base
        self.deadline = deadline
        self.write_deadline = write_deadline
        self.min_bw = min_bw
        self.breaker = breaker or Breaker()
        self.engine: Optional[DeadlineEngine] = None
        # Each EQ is its own CaRT context. per_rank_eq keeps a dead rank's
        # retrying zombies off the context that serves the live ranks.
        self.per_rank_eq = per_rank_eq
        self.slow_poll_secs = slow_poll_secs
        self.placement: Optional[BucketPlacement] = None
        self.ready = threading.Event()
        self.error: Optional[BaseException] = None
        self._ohs: List[object] = []
        threading.Thread(target=self._connect, name="daos-connect", daemon=True).start()

    def _connect(self) -> None:
        from . import obj_binding as ob
        from .daos_event import EventQueue
        try:
            ob._load()
            d = ob._daos
            ob._chk(d.daos_init(), "daos_init")
            poh, coh = ob.DaosHandle(), ob.DaosHandle()
            ob._chk(d.daos_pool_connect(self.pool.encode(),
                                        self.sys.encode() if self.sys else None,
                                        ob.DAOS_PC_RW, ctypes.byref(poh), None, None),
                    "daos_pool_connect")
            ob._chk(d.daos_cont_open(poh, self.cont.encode(), ob.DAOS_COO_RW,
                                     ctypes.byref(coh), None, None), "daos_cont_open")
            d.daos_oclass_name2id.restype = ctypes.c_int
            d.daos_oclass_name2id.argtypes = [ctypes.c_char_p]
            s1 = d.daos_oclass_name2id(b"S1")
            if s1 <= 0:
                raise RuntimeError("oclass S1 unknown to this libdaos")
            d.daos_obj_layout_get.restype = ctypes.c_int
            d.daos_obj_layout_get.argtypes = [ob.DaosHandle, ob.DaosObjId,
                                              ctypes.POINTER(ctypes.c_void_p)]
            d.daos_obj_layout_free.restype = ctypes.c_int
            d.daos_obj_layout_free.argtypes = [ctypes.c_void_p]
            ranks = []
            for b in range(self.nbuckets):
                oid = ob.DaosObjId(self.oid_base + b, 0)
                ob._chk(d.daos_obj_generate_oid(coh, ctypes.byref(oid), 0, s1, 0, 0),
                        "daos_obj_generate_oid")
                oh = ob.DaosHandle()
                ob._chk(d.daos_obj_open(coh, oid, ob.DAOS_OO_RW, ctypes.byref(oh), None),
                        "daos_obj_open")
                lay = ctypes.c_void_p()
                ob._chk(d.daos_obj_layout_get(coh, oid, ctypes.byref(lay)),
                        "daos_obj_layout_get")
                try:
                    ranks.append(_first_rank(lay))
                finally:
                    d.daos_obj_layout_free(lay)
                self._ohs.append(oh)
            self._poh, self._coh = poh, coh
            self.placement = BucketPlacement(self.nbuckets, lambda b: ranks[b])
            rank_eqs = ({r: EventQueue() for r in sorted(set(ranks))}
                        if self.per_rank_eq else None)
            self.engine = DeadlineEngine(EventQueue(), self.breaker, rank_eqs=rank_eqs,
                                         slow_poll_secs=self.slow_poll_secs)
            self.ready.set()
            logger.info("S1BucketStore ready: %d buckets over ranks %s",
                        self.nbuckets, sorted(set(ranks)))
        except BaseException as e:            # surfaced through .error
            self.error = e
            logger.exception("S1BucketStore connect failed")

    def _op(self, dkey: bytes, items, single: bool, fetch: bool, on_reap=None):
        from . import obj_binding as ob
        if not self.ready.is_set():
            return NOT_READY, None
        b = self.placement.bucket(dkey)
        rank = self.placement.rank(b)
        oh = self._ohs[b]
        dk_iov, dk_buf = ob.build_key(dkey)
        iods, sgls, n, keep = ob.build_vectors(items, single=single)
        nbytes = sum(int(x[2]) for x in items)
        hold = (dk_iov, dk_buf, iods, sgls, keep, items)
        d = ob._daos
        if fetch:
            def submit(slot):
                return d.daos_obj_fetch(oh, ob.DaosHandle(0), 0, ctypes.byref(dk_iov), n,
                                        iods, sgls, None, slot.addr)
        else:
            def submit(slot):
                return d.daos_obj_update(oh, ob.DaosHandle(0), 0, ctypes.byref(dk_iov), n,
                                         iods, sgls, slot.addr)
        dl = op_deadline(self.deadline if fetch else self.write_deadline, nbytes,
                         self.min_bw)
        rc = self.engine.run(rank, submit, hold, nbytes, dl, on_reap)
        if rc == 0 and fetch:
            return 0, [int(sgls[i].sg_iovs[0].iov_len) if sgls[i].sg_nr_out else 0
                       for i in range(n)]
        return rc, None

    def fetch(self, dkey: bytes, items: Sequence[Tuple[bytes, object, int]],
              single: bool = False, on_reap=None):
        """Returns ``(rc, got)``. Anything but rc 0 is a miss to the caller;
        ``got`` gates presence the same way ObjSys.fetch does. An aborted
        fetch still writes into its buffers when the rank returns (check 2a);
        ``on_reap`` is when they are free again."""
        return self._op(dkey, items, single, fetch=True, on_reap=on_reap)

    def update(self, dkey: bytes, items: Sequence[Tuple[bytes, object, int]],
               single: bool = False):
        """Returns rc. A put that is not 0 is dropped -- it is a cache."""
        return self._op(dkey, items, single, fetch=False)[0]

    def punch(self, dkey: bytes, akeys: Optional[Sequence[bytes]] = None):
        """Remove a dkey, or only the named akeys under it. Returns rc. An
        aborted punch still lands when the rank returns; for a cache that is
        a late eviction."""
        from . import obj_binding as ob
        if not self.ready.is_set():
            return NOT_READY
        b = self.placement.bucket(dkey)
        oh = self._ohs[b]
        dk_iov, dk_buf = ob.build_key(dkey)
        keep = [dk_iov, dk_buf]
        d = ob._daos
        if akeys:
            arr = (ob.DIov * len(akeys))()
            for i, ak in enumerate(akeys):
                arr[i], buf = ob.build_key(ak)
                keep.append(buf)
            keep.append(arr)

            def submit(slot):
                return d.daos_obj_punch_akeys(oh, ob.DaosHandle(0), 0, ctypes.byref(dk_iov),
                                              len(akeys), arr, slot.addr)
        else:
            def submit(slot):
                return d.daos_obj_punch_dkeys(oh, ob.DaosHandle(0), 0, 1,
                                              ctypes.byref(dk_iov), slot.addr)
        return self.engine.run(self.placement.rank(b), submit, keep, 0,
                               self.write_deadline)

    def close(self) -> None:
        """Stop polling. Handles stay open and zombies stay referenced, for
        the reason DeadlineEngine.close gives."""
        if self.engine is not None:
            left = self.engine.close()
            if left:
                logger.warning("S1BucketStore closed with %d zombies outstanding", left)

    def snapshot(self) -> dict:
        eng = self.engine
        return {"ready": self.ready.is_set(), "ranks": self.breaker.snapshot(),
                "stats": dict(eng.stats) if eng else {},
                "zombies": eng.zombies() if eng else 0,
                "poll_max": eng.poll_max() if eng else {}}


# ---- connector adapter ------------------------------------------------------
class Parked(Exception):
    """A fetch hit its deadline, and its destination now belongs to a zombie.

    DAOS still writes into it when the rank returns (check 2a), so the caller
    must neither reuse nor release it. The ``release`` given to the fetch runs
    once DAOS has let go.
    """


class Dropped(Exception):
    """A write or punch that was not done: skipped, past its deadline, store
    not ready, or a DAOS error. One past its deadline may still land when the
    rank returns (check 2a), which is harmless only for keys whose value never
    changes -- this cache's keys are content hashes.
    """

    def __init__(self, what: str, rc):
        super().__init__("%s: %s" % (what, rc))
        self.rc = rc


class GuardedObj:
    """ObjSys's interface over an :class:`S1BucketStore`, for DaosConnector.

    The connector's raw path stays as it is; what changes is how a request
    that cannot be served ends. A fetch that is skipped, aborted or not ready
    reports 0 bytes for every akey, which the connector already reads as a
    miss. A write or punch raises :class:`Dropped`, so the caller does not
    commit metadata over a payload that did not land.
    """

    def __init__(self, store: "S1BucketStore"):
        self.store = store

    def _check(self) -> None:
        if self.store.error is not None:      # connect failed for good: say so
            raise self.store.error

    def fetch(self, dkey: bytes, items: Sequence[Tuple[bytes, object, int]],
              single: bool = False, release: Optional[Callable[[], None]] = None
              ) -> List[int]:
        """``release`` frees the destination buffers. Pass it when they are not
        the caller's to keep (an allocator's): on a deadline it is deferred to
        the reap and :class:`Parked` is raised instead of returning."""
        self._check()
        on_reap = (lambda rc: release()) if release is not None else None
        rc, got = self.store.fetch(dkey, items, single=single, on_reap=on_reap)
        if rc == 0:
            return got
        if rc == DEADLINE and release is not None:
            raise Parked(dkey)
        return [0] * len(items)

    def update(self, dkey: bytes, items: Sequence[Tuple[bytes, object, int]],
               single: bool = False) -> None:
        self._check()
        rc = self.store.update(dkey, items, single=single)
        if rc != 0:
            raise Dropped("update", rc)

    def punch(self, dkey: bytes) -> None:
        self._check()
        rc = self.store.punch(dkey)
        if rc != 0:
            raise Dropped("punch", rc)

    def punch_akeys(self, dkey: bytes, akeys: Sequence[bytes]) -> None:
        if not akeys:
            return
        self._check()
        rc = self.store.punch(dkey, akeys)
        if rc != 0:
            raise Dropped("punch_akeys", rc)

    def close(self) -> None:
        self.store.close()
