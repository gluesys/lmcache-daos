# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Read bandwidth of the rank-guard object path against DFS (moon D5b).

The MP adapter's two backends with the calls it makes (stat_size, then
read_obj_into into an L1-sized buffer), plus a breakdown of the guarded path:
the length fetch alone, the bounce allocation alone, and the data fetch alone
into a caller buffer.

    python tests/bench_guard_bw.py POOL DFS_CONT OBJ_CONT [--keys 40]
        [--mib 203] [--workers 1,16] [--oclass S1] [--buckets 64]
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lmcache_daos.dfs_binding import DfsSys  # noqa: E402
from lmcache_daos.mp.guarded_kv import AKEY_DATA, GuardedKV  # noqa: E402
from lmcache_daos.rank_guard import Breaker, S1BucketStore  # noqa: E402


def run(fn, items, workers):
    t = time.monotonic()
    with cf.ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(fn, items))
    return time.monotonic() - t, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pool")
    ap.add_argument("dfs_cont")
    ap.add_argument("obj_cont")
    ap.add_argument("--sys", default=os.environ.get("DAOS_SYSTEM"))
    ap.add_argument("--keys", type=int, default=40)
    ap.add_argument("--mib", type=float, default=203)
    ap.add_argument("--workers", default="1,16")
    ap.add_argument("--oclass", default="S1")
    ap.add_argument("--buckets", type=int, default=64)
    ap.add_argument("--only", default="dfs,guard")
    a = ap.parse_args()

    n = int(a.mib * (1 << 20))
    src = (ctypes.c_char * n)()
    ctypes.memset(src, 0x5A, n)
    dst = [(ctypes.c_char * n)() for _ in range(max(int(w) for w in a.workers.split(",")))]
    names = ["/bw/k%04d" % i for i in range(a.keys)]
    gb = a.keys * n / 1e9
    print("keys=%d size=%.0f MiB total=%.2f GB oclass=%s buckets=%d"
          % (a.keys, a.mib, gb, a.oclass, a.buckets), flush=True)

    backends = {}
    if "dfs" in a.only:
        dfs = DfsSys(pool=a.pool, cont=a.dfs_cont, sys=a.sys)
        dfs.mkdir_p("/bw")
        backends["dfs"] = dfs
    if "guard" in a.only:
        st = S1BucketStore(a.pool, a.obj_cont, sys=a.sys, nbuckets=a.buckets,
                           oclass=a.oclass, breaker=Breaker(max_open_secs=60.0))
        if not st.ready.wait(60):
            raise SystemExit("guard store not ready: %r" % st.error)
        g = GuardedKV(st)
        backends["guard"] = g
        if a.oclass == "S1":
            pl = st.placement
            use = collections.Counter(pl.rank(pl.bucket(g._dkey(p))) for p in names)
            print("guard: keys per rank %s" % dict(sorted(use.items())), flush=True)

    for name, be in backends.items():
        def put(p, be=be):
            h = be.open_rdwr_create(p)
            try:
                return be.write_obj_from(h, 0, n, src)
            finally:
                be.close_obj(h)
        el, res = run(put, names, 16)
        print("%-5s write  W=16: %6.2f s  %5.2f GB/s  ok=%d/%d"
              % (name, el, gb / el, sum(r == n for r in res), a.keys), flush=True)

    for w in [int(x) for x in a.workers.split(",")]:
        for name, be in backends.items():
            slots = {}

            def get(p, be=be):
                i = slots.setdefault(p, len(slots) % len(dst))
                h = be.open_rdonly(p)
                try:
                    return be.read_obj_into(h, 0, n, dst[i])
                finally:
                    be.close_obj(h)
            el, res = run(get, names, w)
            print("%-5s read   W=%-2d: %6.2f s  %5.2f GB/s  ok=%d/%d"
                  % (name, w, el, gb / el, sum(r == n for r in res), a.keys), flush=True)

        g = backends.get("guard")
        if g is None:
            continue
        if "dfs" in backends:
            el, _ = run(backends["dfs"].stat_size, names, w)
            print("  dfs   stat only      W=%-2d: %6.3f s (%.1f ms/key)"
                  % (w, el, el / a.keys * 1e3 * w), flush=True)
        el, _ = run(g.stat_size, names, w)
        print("  guard stat only      W=%-2d: %6.3f s (%.1f ms/key)"
              % (w, el, el / a.keys * 1e3 * w), flush=True)

        def bounce(_):
            b = g._bounce.get(n)
            g._bounce.put(b)
            return len(b)
        el, _ = run(bounce, names, w)
        print("  guard bounce get+put W=%-2d: %6.3f s (%.1f ms/key)"
              % (w, el, el / a.keys * 1e3 * w), flush=True)

        slots = {}

        def raw(p):
            i = slots.setdefault(p, len(slots) % len(dst))
            (got,) = g._obj.fetch(g._dkey(p), [(AKEY_DATA, dst[i], n)])
            return got
        el, res = run(raw, names, w)
        print("  guard raw fetch      W=%-2d: %6.2f s  %5.2f GB/s  ok=%d/%d"
              % (w, el, gb / el, sum(r == n for r in res), a.keys), flush=True)

    for be in backends.values():
        for p in names:
            try:
                be.remove(p)
            except Exception:
                pass
    if "guard" in backends:
        print("guard engine stats %s" % backends["guard"].store.engine.stats, flush=True)
        backends["guard"].close()


if __name__ == "__main__":
    main()
