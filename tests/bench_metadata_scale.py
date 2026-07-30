"""How do DFS metadata operations scale with object count in one flat directory?

`_key_to_path` puts every chunk in the container root as `/<sha256>`, and the
design doc warns that past a few hundred thousand files "metadata and directory
distribution become the bottleneck before data bandwidth". Nothing measured so
far has come close to that regime -- the Phase 3 runs touched 19 objects -- so
this walks the object count up and watches lookup, create, read and readdir.

Deliberately uses small payloads and talks to DfsSys directly rather than through
the connector: the point is to isolate the *metadata* term from data transfer and
from LMCache overhead. Real chunks are 28 MiB, where transfer dominates at low
counts; what matters here is whether the per-object metadata cost stays flat.

SAFETY -- this consumes pool metadata, which in MD-on-SSD mode is capped by the
engines' ram-disk (nvme_pool: 503 MB total). The run aborts if metadata or data
headroom drops below the floors below, and it uses a dedicated container so the
space can be reclaimed by destroying it.

    POOL=nvme_pool CONT=lmcache_scale python3 tests/bench_metadata_scale.py
"""

import os
import random
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmcache_daos.dfs_binding import DfsSys  # noqa: E402

POOL = os.environ.get("POOL", "nvme_pool")
CONT = os.environ.get("CONT", "lmcache_scale")
PAYLOAD = int(os.environ.get("PAYLOAD_B", "1024"))
TARGETS = [int(x) for x in os.environ.get(
    "TARGETS", "1000,5000,20000,50000").split(",")]
SAMPLE = int(os.environ.get("SAMPLE", "300"))
FILL_THREADS = int(os.environ.get("FILL_THREADS", "16"))
META_FLOOR_MB = int(os.environ.get("META_FLOOR_MB", "150"))
DATA_FLOOR_MB = int(os.environ.get("DATA_FLOOR_MB", "2000"))

BLOB = bytes(random.Random(7).getrandbits(8) for _ in range(PAYLOAD))


def pool_free_mb():
    """(metadata_free_mb, data_free_mb) from `daos pool query`, or (None, None)."""
    try:
        out = subprocess.run(["daos", "pool", "query", POOL],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None, None
    frees = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Free:"):
            tok = line.split()[1:3]          # e.g. ["415", "MB,"]
            try:
                val = float(tok[0])
            except ValueError:
                continue
            unit = tok[1].rstrip(",").upper()
            frees.append(val * (1024 if unit.startswith("G") else 1))
    # `daos pool query` prints metadata first, then data
    if len(frees) >= 2:
        return frees[0], frees[1]
    return None, None


def name(i):
    return f"/obj{i:08d}"


def stats_ms(samples):
    s = sorted(samples)
    return (s[0] * 1000, statistics.mean(s) * 1000,
            s[int(len(s) * 0.95) - 1] * 1000)


def main():
    dfs = DfsSys(pool=POOL, cont=CONT)
    have = 0
    print(f"payload={PAYLOAD} B  targets={TARGETS}  sample={SAMPLE}")
    m, d = pool_free_mb()
    print(f"pool free at start: meta={m} MB data={d} MB")
    try:
        for target in TARGETS:
            # ---- fill up to target ----
            t0 = time.perf_counter()
            todo = range(have, target)
            with ThreadPoolExecutor(max_workers=FILL_THREADS) as ex:
                list(ex.map(lambda i: dfs.write(name(i), BLOB), todo))
            fill = time.perf_counter() - t0
            created = target - have
            have = target
            rate = created / fill if fill else 0

            m, d = pool_free_mb()
            print(f"\n=== {have} objects ===")
            print(f"  fill: +{created} in {fill:.1f}s ({rate:.0f} obj/s)"
                  f"   pool free meta={m} MB data={d} MB")

            idx = random.Random(target).sample(range(have), min(SAMPLE, have))

            # ---- exists: hit ----
            ts = []
            for i in idx:
                t = time.perf_counter(); dfs.exists(name(i)); ts.append(time.perf_counter() - t)
            lo, avg, p95 = stats_ms(ts)
            print(f"  exists  HIT : min {lo:.3f}  mean {avg:.3f}  p95 {p95:.3f} ms")

            # ---- exists: miss ----
            ts = []
            for i in idx:
                t = time.perf_counter(); dfs.exists(f"/absent{i:08d}"); ts.append(time.perf_counter() - t)
            lo, avg, p95 = stats_ms(ts)
            print(f"  exists  MISS: min {lo:.3f}  mean {avg:.3f}  p95 {p95:.3f} ms")

            # ---- read ----
            ts = []
            for i in idx[:100]:
                t = time.perf_counter(); dfs.read(name(i), 0, PAYLOAD); ts.append(time.perf_counter() - t)
            lo, avg, p95 = stats_ms(ts)
            print(f"  read        : min {lo:.3f}  mean {avg:.3f}  p95 {p95:.3f} ms")

            # ---- create new (the metadata-mutating path) ----
            ts = []
            for j in range(200):
                p = f"/probe{target}_{j:05d}"
                t = time.perf_counter(); dfs.write(p, BLOB); ts.append(time.perf_counter() - t)
            lo, avg, p95 = stats_ms(ts)
            print(f"  create      : min {lo:.3f}  mean {avg:.3f}  p95 {p95:.3f} ms")
            for j in range(200):
                dfs.remove(f"/probe{target}_{j:05d}")

            # ---- readdir (full enumeration) ----
            t0 = time.perf_counter()
            n = sum(1 for _ in dfs.iterdir("/"))
            rd = time.perf_counter() - t0
            print(f"  readdir     : {n} entries in {rd*1000:.1f} ms"
                  f"  ({rd/max(n,1)*1e6:.1f} us/entry)")

            if m is not None and m < META_FLOOR_MB:
                print(f"\nSTOP: pool metadata free {m} MB below floor {META_FLOOR_MB} MB")
                break
            if d is not None and d < DATA_FLOOR_MB:
                print(f"\nSTOP: pool data free {d} MB below floor {DATA_FLOOR_MB} MB")
                break
    finally:
        print(f"\nleaving {have} objects in {POOL}/{CONT}; "
              f"reclaim with: daos cont destroy {POOL} {CONT}")
        dfs.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
