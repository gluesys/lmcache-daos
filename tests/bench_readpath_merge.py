# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Merge decision: main's read path vs this branch's, on the same objects.

The two lines of work wrote `_get_sync` differently and both choices are
defensible on their own terms:

  main            exists() + read(prefix) + read(meta) + read(payload), each a
                  path-based helper that opens the file again, then copies the
                  payload bytes into the MemoryObj. Every length is checked, so
                  a torn object (writer killed mid-store) is reported as a plain
                  miss rather than raising -- which matters because vLLM's
                  default kv_load_failure_policy is `fail`, not recompute.

  this branch     one open, one 520 B header read, then read straight into the
                  MemoryObj buffer via read_obj_into. No intermediate bytes, no
                  second copy, and the ctypes call releases the GIL so parallel
                  chunk reads actually overlap. No torn-object handling.

So: 4 opens + 2 full copies + safe, versus 1 open + 0 copies + unsafe. This
measures the cost of that difference at the real chunk size so the merge can
take main's safety without inheriting its overhead (arm 3).

  arm A  main-style      exists + 3 path reads + copy into destination
  arm B  branch-style    1 open + header read + read_obj_into
  arm C  proposed        1 open + header read + read_obj_into, with the length
                         checks kept -- read_obj_into already returns the byte
                         count, so a short read is detectable without
                         materialising the payload

    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16 \
        python3 /lmd/tests/bench_readpath_merge.py
"""

import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, "/lmd")

from lmcache_daos import serde
from lmcache_daos.dfs_binding import DfsSys

POOL = os.environ["DAOS_TEST_POOL"]
CONT = os.environ["DAOS_TEST_CONT"]
SZ = int(os.environ.get("SZ", 29360128))          # 28 MiB
N = int(os.environ.get("N", 32))
THREADS = [int(x) for x in os.environ.get("THREADS", "1,16").split(",")]
REPS = int(os.environ.get("REPS", 3))

# On-disk layout: [prefix 8B][meta][payload]. Use a meta blob of a realistic
# size (~28 B for a KV chunk) so the header read is representative.
META = b"m" * 28
HDR = serde.prefix_pack(len(META), SZ) + META
HOFF = len(HDR)

setup = DfsSys(pool=POOL, cont=CONT)
try:
    payload = ctypes.create_string_buffer(bytes([0x7E]) * SZ, SZ)
    hdrbuf = ctypes.create_string_buffer(HDR, len(HDR))
    for i in range(N):
        o = setup.open_rdwr_create(f"/rp_{i}")
        try:
            setup.write_obj_from(o, 0, len(HDR), hdrbuf)
            setup.write_obj_from(o, len(HDR), SZ, payload)
        finally:
            setup.close_obj(o)
    print(f"corpus {N} x {SZ>>20} MiB (+{HOFF} B header) = {N*SZ/1e9:.2f} GB, "
          f"best of {REPS}\n", flush=True)

    def arm_main(d, i, dest, mv):
        """4 opens, payload materialised as bytes, then copied in."""
        path = f"/rp_{i}"
        if not d.exists(path):                                   # open 1
            return None
        prefix = d.read(path, 0, serde.prefix_size())            # open 2
        if len(prefix) != serde.prefix_size():
            return None
        meta_len, payload_len = serde.parse_prefix(prefix)
        meta = d.read(path, serde.prefix_size(), meta_len)        # open 3
        if len(meta) != meta_len:
            return None
        kv = d.read(path, serde.prefix_size() + meta_len, payload_len)  # open 4
        if len(kv) != payload_len:
            return None
        mv[:payload_len] = kv                                    # copy 2
        return payload_len

    def arm_branch(d, i, dest, mv):
        """1 open, header read, straight into the destination buffer."""
        obj = d.open_rdonly(f"/rp_{i}")
        try:
            ps = serde.prefix_size()
            hdr = d.read_obj(obj, 0, ps + 512)
            meta_len, payload_len = serde.parse_prefix(hdr[:ps])
            d.read_obj_into(obj, ps + meta_len, payload_len, dest)
            return payload_len
        finally:
            d.close_obj(obj)

    def arm_proposed(d, i, dest, mv):
        """Same as branch, but every length checked -> torn object = miss."""
        try:
            obj = d.open_rdonly(f"/rp_{i}")
        except Exception:
            return None                       # absent
        try:
            ps = serde.prefix_size()
            hdr = d.read_obj(obj, 0, ps + 512)
            if len(hdr) < ps:
                return None                   # empty or mid-prefix
            meta_len, payload_len = serde.parse_prefix(hdr[:ps])
            if len(hdr) < ps + meta_len:
                return None                   # mid-metadata
            got = d.read_obj_into(obj, ps + meta_len, payload_len, dest)
            if got != payload_len:
                return None                   # truncated payload
            return got
        finally:
            d.close_obj(obj)

    ARMS = (("A main-style", arm_main),
            ("B branch-style", arm_branch),
            ("C proposed", arm_proposed))

    def run(fn, nthreads):
        shard = [[i for i in range(N) if i % nthreads == k]
                 for k in range(nthreads)]
        errs = []
        barrier = threading.Barrier(nthreads + 1)

        def worker(k):
            try:
                d = DfsSys(pool=POOL, cont=CONT)
                dest = ctypes.create_string_buffer(SZ)
                dest[0] = 0
                dest[SZ - 1] = 0
                mv = memoryview(dest).cast("B")
                barrier.wait()
                for i in shard[k]:
                    if fn(d, i, dest, mv) is None:
                        errs.append(f"miss on {i}")
                d.close()
            except Exception as e:
                errs.append(f"{type(e).__name__}: {e}")
                try:
                    barrier.wait()
                except Exception:
                    pass

        th = [threading.Thread(target=worker, args=(k,))
              for k in range(nthreads)]
        for x in th:
            x.start()
        barrier.wait()
        t0 = time.perf_counter()
        for x in th:
            x.join(timeout=600)
        return N * SZ / (time.perf_counter() - t0) / 1e9, errs

    hdr = "".join(f"{t:>12}" for t in THREADS)
    print(f"{'arm':<16}{hdr}   note", flush=True)
    base = {}
    for name, fn in ARMS:
        cells, note = [], ""
        for t in THREADS:
            best, errs = 0.0, []
            for _ in range(REPS):
                g, e = run(fn, t)
                best = max(best, g)
                errs += e
            cells.append(f"{best:>12.2f}")
            base.setdefault(name, {})[t] = best
            if errs and not note:
                note = f"  {len(errs)} err: {errs[0][:34]}"
        print(f"{name:<16}{''.join(cells)}{note}", flush=True)

    print(flush=True)
    for t in THREADS:
        a, b, c = (base["A main-style"][t], base["B branch-style"][t],
                   base["C proposed"][t])
        print(f"{t:>2} threads: B/A = {b/a:.2f}x, C/A = {c/a:.2f}x, "
              f"C/B = {c/b:.2f}x  (C keeps main's checks)", flush=True)
finally:
    for i in range(N):
        try:
            setup.remove(f"/rp_{i}")
        except Exception:
            pass
    setup.close()
