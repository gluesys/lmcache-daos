#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""ReuseStats counts without DAOS: a fake clock drives writes, hits, misses
and evictions, and the snapshot must say which misses a lower tier would have
served (ghost misses) and how long after eviction.

    python3 tests/test_reuse_stats.py
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmcache_daos.reuse_stats import ReuseStats     # noqa: E402

fails = 0


def ck(what, ok, extra=""):
    global fails
    if not ok:
        fails += 1
    print("  %-62s %s %s" % (what, "PASS" if ok else "FAIL", extra))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def counts_and_gaps():
    c = Clock()
    r = ReuseStats(60, clock=c)
    r.written(b"a", 100)
    r.written(b"b", 200)
    c.t += 5
    r.read(b"a")                 # 5 s after write
    c.t += 120
    r.read(b"a")                 # 120 s after previous hit
    r.read(b"x", 50)             # written by someone else
    r.missed(b"y")
    s = r.snapshot()
    ck("writes / bytes", (s["writes"], s["write_bytes"]) == (2, 300))
    ck("hits / bytes (known size used)", (s["hits"], s["hit_bytes"]) == (3, 250), s["hit_bytes"])
    ck("hit by another writer is unknown", s["hits_unknown"] == 1)
    ck("gap buckets", s["hit_gap"]["<=10s"] == 1 and s["hit_gap"]["<=600s"] == 1, s["hit_gap"])
    ck("plain miss is not a ghost miss", (s["misses"], s["ghost_misses"]) == (1, 0))
    ck("live reads histogram", s["reads_live"] == {"<=0": 1, "<=1": 1, "<=2": 1, "<=4": 0,
                                                  "<=8": 0, ">8": 0}, s["reads_live"])


def ghost_misses():
    c = Clock()
    r = ReuseStats(60, clock=c)
    r.written(b"a", 100)
    r.read(b"a")
    r.read(b"a")
    r.written(b"b", 300)
    r.evicted(b"a")
    r.evicted(b"b")
    c.t += 30
    r.missed(b"a")               # asked for again 30 s after eviction
    c.t += 7200
    r.missed(b"b")               # 2 h after
    s = r.snapshot()
    ck("evictions / bytes", (s["evictions"], s["evicted_bytes"]) == (2, 400))
    ck("reads at eviction", s["reads_at_evict"]["<=0"] == 1 and s["reads_at_evict"]["<=2"] == 1,
       s["reads_at_evict"])
    ck("ghost misses / bytes", (s["ghost_misses"], s["ghost_miss_bytes"]) == (2, 400))
    ck("ghost miss age", s["ghost_miss_age"]["<=60s"] == 1 and s["ghost_miss_age"][">3600s"] == 1,
       s["ghost_miss_age"])
    r.written(b"a", 100)
    s = r.snapshot()
    ck("rewrite after eviction leaves the ghost", (s["rewrites_after_evict"], s["ghost_keys"]) == (1, 1))
    r.missed(b"a")
    ck("a live key's miss is not a ghost miss", r.snapshot()["ghost_misses"] == 2)


def ghost_bound():
    r = ReuseStats(60, ghost_max=3, clock=Clock())
    for k in range(5):
        r.written(k, 1)
        r.evicted(k)
    r.missed(0)
    r.missed(4)
    s = r.snapshot()
    ck("ghost keeps the newest only", (s["ghost_keys"], s["ghost_misses"]) == (3, 1))


def logs_on_interval():
    c = Clock()
    r = ReuseStats(60, clock=c)
    got = []

    class H(logging.Handler):
        def emit(self, rec):
            got.append(rec.getMessage())
    lg = logging.getLogger("t_reuse")
    lg.addHandler(H())
    lg.setLevel(logging.INFO)
    lg.propagate = False
    r.maybe_log(lg)
    ck("nothing before the interval", got == [])
    c.t += 61
    r.written(b"a", 10)
    r.maybe_log(lg)
    r.maybe_log(lg)
    ck("one line per interval", len(got) == 1, len(got))
    body = got[0].split("DAOS reuse stats ", 1)[1] if got else "{}"
    ck("line is JSON with the counters", json.loads(body).get("writes") == 1)


def main():
    for t in (counts_and_gaps, ghost_misses, ghost_bound, logs_on_interval):
        print(t.__name__)
        t()
    print("FAIL" if fails else "OK", "(%d failed)" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
