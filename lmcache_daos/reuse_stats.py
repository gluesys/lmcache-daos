# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""What a lower tier would buy, measured from the cache's own traffic.

Tiering (moving evicted chunks to a cheaper store instead of dropping them,
doc/EVICTION-DESIGN.md and the 10-23 discussion) pays only if evicted chunks
are asked for again, and soon enough to matter. The lane workloads cannot say
whether they are; a real service can. So the connector counts, per process:

  * writes, hits, misses, with bytes;
  * the gap between a chunk's previous access and each hit;
  * at eviction, how many times the chunk had been read;
  * **misses on chunks this process evicted** ("ghost misses"), with bytes and
    the time since eviction -- each one is a hit a lower tier would have
    served instead of a recompute.

``DAOS_REUSE_STATS=<seconds>`` turns it on and logs one JSON line that often
(``DAOS reuse stats {...}``, cumulative since start). Off by default: a dict
entry per stored chunk is the cost. ``DAOS_REUSE_STATS_GHOST`` bounds how many
evicted keys are remembered (default 100000, oldest forgotten first).

A chunk is one dkey on the object path (every layer of a layerwise chunk
counts once, as layer 0) and one file on the DFS path. Counts are this
process's view: a chunk another process wrote is "unknown" until this one
writes or reads it.
"""
from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict

# upper bounds, seconds / reads; the last bucket is open
GAP_EDGES = (10, 60, 600, 3600)
READ_EDGES = (0, 1, 2, 4, 8)


def _bucket(edges, v) -> int:
    for i, e in enumerate(edges):
        if v <= e:
            return i
    return len(edges)


def _labels(edges, unit):
    out = ["<=%g%s" % (e, unit) for e in edges]
    return out + [">%g%s" % (edges[-1], unit)]


class ReuseStats:
    def __init__(self, interval: float, ghost_max: int = 100000, clock=time.monotonic):
        self.interval = float(interval)
        self.ghost_max = int(ghost_max)
        self._now = clock
        self._lock = threading.Lock()
        self._live = {}                       # key -> [last_access, reads, nbytes]
        self._ghost: "OrderedDict" = OrderedDict()   # key -> (evicted_at, nbytes, reads)
        self._t0 = clock()
        self._last_log = self._t0
        self.c = dict(writes=0, write_bytes=0, rewrites_after_evict=0,
                      hits=0, hit_bytes=0, hits_unknown=0,
                      misses=0, ghost_misses=0, ghost_miss_bytes=0,
                      evictions=0, evicted_bytes=0)
        self.gap = [0] * (len(GAP_EDGES) + 1)          # hit: time since previous access
        self.ghost_age = [0] * (len(GAP_EDGES) + 1)    # ghost miss: time since eviction
        self.reads_at_evict = [0] * (len(READ_EDGES) + 1)

    def written(self, key, nbytes: int) -> None:
        now = self._now()
        with self._lock:
            self.c["writes"] += 1
            self.c["write_bytes"] += nbytes
            g = self._ghost.pop(key, None)
            if g is not None:
                self.c["rewrites_after_evict"] += 1
            e = self._live.get(key)
            if e is None:
                self._live[key] = [now, 0, nbytes]
            else:
                e[0], e[2] = now, nbytes

    def read(self, key, nbytes: int = 0) -> None:
        now = self._now()
        with self._lock:
            self.c["hits"] += 1
            e = self._live.get(key)
            if e is None:
                self.c["hits_unknown"] += 1
                self.c["hit_bytes"] += nbytes
                self._live[key] = [now, 1, nbytes]
                return
            self.gap[_bucket(GAP_EDGES, now - e[0])] += 1
            self.c["hit_bytes"] += nbytes or e[2]
            e[0] = now
            e[1] += 1

    def missed(self, key) -> None:
        now = self._now()
        with self._lock:
            self.c["misses"] += 1
            g = self._ghost.get(key)
            if g is not None:
                self.c["ghost_misses"] += 1
                self.c["ghost_miss_bytes"] += g[1]
                self.ghost_age[_bucket(GAP_EDGES, now - g[0])] += 1

    def evicted(self, key) -> None:
        now = self._now()
        with self._lock:
            e = self._live.pop(key, None)
            nbytes, reads = (e[2], e[1]) if e is not None else (0, 0)
            self.c["evictions"] += 1
            self.c["evicted_bytes"] += nbytes
            self.reads_at_evict[_bucket(READ_EDGES, reads)] += 1
            self._ghost[key] = (now, nbytes, reads)
            self._ghost.move_to_end(key)
            while len(self._ghost) > self.ghost_max:
                self._ghost.popitem(last=False)

    def snapshot(self) -> dict:
        with self._lock:
            live_reads = [0] * (len(READ_EDGES) + 1)
            for e in self._live.values():
                live_reads[_bucket(READ_EDGES, e[1])] += 1
            return dict(
                elapsed_s=round(self._now() - self._t0, 1),
                **self.c,
                live_keys=len(self._live), ghost_keys=len(self._ghost),
                hit_gap=dict(zip(_labels(GAP_EDGES, "s"), self.gap)),
                ghost_miss_age=dict(zip(_labels(GAP_EDGES, "s"), self.ghost_age)),
                reads_at_evict=dict(zip(_labels(READ_EDGES, ""), self.reads_at_evict)),
                reads_live=dict(zip(_labels(READ_EDGES, ""), live_reads)))

    def maybe_log(self, logger) -> None:
        """Log a snapshot if ``interval`` has passed; cheap to call often."""
        now = self._now()
        if now - self._last_log < self.interval:
            return
        with self._lock:
            if now - self._last_log < self.interval:
                return
            self._last_log = now
        logger.info("DAOS reuse stats %s", json.dumps(self.snapshot(), separators=(",", ":")))
