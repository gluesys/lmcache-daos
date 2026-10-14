#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""S1BucketStore against a real node outage (run on a client, not in CI).

Writes ``--keys`` 64 KiB values, then runs a get/put mix for ``--secs`` and
prints one line per second: per-rank breaker state, zombies, and how that
second's gets ended (hit, miss, deadline, skipped) with their worst latency.
Power a storage node off and on while it runs; the driver lives in
daos-reboot-repro. A pool for the ``kvcache`` profile is assumed: rd_fac 0,
``self_heal:none``.

    python3 tests/rank_guard_outage.py POOL CONT --keys 600 --secs 900

What a pass looks like:
  * during the outage, gets on the live ranks keep hitting at the healthy
    latency, and gets on the dead rank end within one deadline, then skip;
  * the dead rank's zombies stop at the breaker's cap or below;
  * after the rank returns, its zombies are reaped and its keys hit again.
"""
import argparse
import collections
import ctypes
import json
import logging
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmcache_daos import rank_guard as G     # noqa: E402

VSIZE = 65536
AKEY = b"v"


def value(i: int) -> bytes:
    return (i.to_bytes(8, "little") * (VSIZE // 8))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pool")
    ap.add_argument("cont")
    ap.add_argument("--keys", type=int, default=600)
    ap.add_argument("--secs", type=float, default=900)
    ap.add_argument("--deadline", type=float, default=0.5)
    ap.add_argument("--buckets", type=int, default=64)
    ap.add_argument("--put-ratio", type=float, default=0.1)
    ap.add_argument("--connect-wait", type=float, default=60)
    ap.add_argument("--one-eq", action="store_true", help="all ranks on one EQ (run A)")
    ap.add_argument("--slow-poll", type=float, default=0.05,
                    help="poll interval for an open rank's EQ; 0 = every round (run C)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    st = G.S1BucketStore(a.pool, a.cont, nbuckets=a.buckets, deadline=a.deadline,
                         per_rank_eq=not a.one_eq, slow_poll_secs=a.slow_poll)
    t0 = time.monotonic()
    if not st.ready.wait(a.connect_wait):
        print(json.dumps({"event": "not_ready", "after": a.connect_wait}), flush=True)
        return 2
    if st.error:
        raise st.error
    print(json.dumps({"event": "ready", "secs": round(time.monotonic() - t0, 2)}),
          flush=True)

    keys = [b"k%08d" % i for i in range(a.keys)]
    for i, k in enumerate(keys):
        buf = ctypes.create_string_buffer(value(i), VSIZE)
        rc = st.update(k, [(AKEY, buf, VSIZE)], single=True)
        if rc != 0:
            print(json.dumps({"event": "seed_fail", "key": i, "rc": rc}), flush=True)
            return 1
    rank_of = {i: st.placement.rank(st.placement.bucket(k)) for i, k in enumerate(keys)}
    print(json.dumps({"event": "seeded", "per_rank":
                      collections.Counter(rank_of.values())}), flush=True)

    rnd = random.Random(1)
    end = time.monotonic() + a.secs
    bad = 0
    while time.monotonic() < end:
        sec_end = time.monotonic() + 1.0
        out = collections.defaultdict(collections.Counter)
        worst = collections.defaultdict(float)
        lat = collections.defaultdict(list)
        while time.monotonic() < sec_end:
            i = rnd.randrange(a.keys)
            r = rank_of[i]
            if rnd.random() < a.put_ratio:
                buf = ctypes.create_string_buffer(value(i), VSIZE)
                s = time.monotonic()
                rc = st.update(keys[i], [(AKEY, buf, VSIZE)], single=True)
                out[r]["put_ok" if rc == 0 else "put_" + str(rc)] += 1
            else:
                buf = ctypes.create_string_buffer(VSIZE)
                s = time.monotonic()
                rc, got = st.fetch(keys[i], [(AKEY, buf, VSIZE)], single=True)
                if rc == 0 and got and got[0] == VSIZE:
                    if buf.raw != value(i):
                        bad += 1
                        out[r]["BADDATA"] += 1
                    else:
                        out[r]["hit"] += 1
                elif rc == 0:
                    out[r]["miss"] += 1
                else:
                    out[r][str(rc)] += 1
            worst[r] = max(worst[r], time.monotonic() - s)
            lat[r].append(time.monotonic() - s)
        snap = st.snapshot()
        print(json.dumps({"t": round(time.time(), 1),
                          "ranks": {str(r): {"state": v["state"], "zombies": v["zombies"]}
                                    for r, v in snap["ranks"].items()},
                          "ops": {str(r): dict(c) for r, c in sorted(out.items())},
                          "worst": {str(r): round(v, 3) for r, v in sorted(worst.items())},
                          "p50": {str(r): round(sorted(v)[len(v) // 2], 4)
                                  for r, v in sorted(lat.items())},
                          "zombies": snap["zombies"],
                          "poll_max": {str(r): round(v, 4)
                                       for r, v in snap["poll_max"].items()}}), flush=True)
    print(json.dumps({"event": "end", "stats": st.snapshot()["stats"], "bad": bad}),
          flush=True)
    os._exit(1 if bad else 0)           # zombies may remain; do not run daos_fini


if __name__ == "__main__":
    sys.exit(main())
