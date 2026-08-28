"""Single-node raw read throughput vs working-set size.

Why: Part C claimed the 2-node aggregate (32.8 GB/s, end-to-end vLLM retrieve
over a 100 GB NVMe-resident KV set) had reached the server-side ceiling, citing
a connector read ceiling of ~33.6 GB/s. But bench_async_vs_sync later measured a
*single* client doing 35.3 GB/s raw -- on a 0.94 GB working set, small enough to
sit in server memory. A cached micro-measurement cannot support a claim about
NVMe-resident behaviour, so this sweeps the working set from cache-resident up
to the 100 GB Part C used and reports where, if anywhere, throughput falls off.

Reading of the outcome:
  * if ~33-35 GB/s holds at 100 GB -> one client alone can saturate the shared
    path, so the 2-node aggregate landing at 32.8 GB/s means a SHARED ceiling
    (server/fabric) rather than a client limit. Part C's interpretation stands.
  * if it collapses at 100 GB -> the 35 GB/s was a cache artefact and Part C's
    "NVMe-resident, defensible" framing has to be softened.

Layout: WORKERS files, each FILE_GB, each read sequentially in CHUNK-sized
reads by its own thread on its own dfs_sys handle with a reused buffer. Working
set is varied by reading only a prefix of each file; the largest point spans the
whole corpus so no plausible server cache holds it.

    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=rawbig \
        WORKERS=16 FILE_GB=6.4 SET_GB=2,8,32,100 \
        python3 /lmd/tests/bench_raw_workingset.py
"""

import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, "/lmd")

from lmcache_daos.dfs_binding import DfsSys

POOL = os.environ["DAOS_TEST_POOL"]
CONT = os.environ["DAOS_TEST_CONT"]
WORKERS = int(os.environ.get("WORKERS", 16))
FILE_GB = float(os.environ.get("FILE_GB", 6.4))
CHUNK = int(os.environ.get("CHUNK", 28 * 1024 * 1024))
SETS = [float(x) for x in os.environ.get("SET_GB", "2,8,32,100").split(",")]
REPS = int(os.environ.get("REPS", 2))
SKIP_WRITE = os.environ.get("SKIP_WRITE", "0") == "1"

FILE_BYTES = int(FILE_GB * 1e9 // CHUNK) * CHUNK      # whole chunks
TOTAL_BYTES = FILE_BYTES * WORKERS


def precreate(errs):
    """Create the files serially, from one handle, before any parallel work.

    Creating them concurrently from 16 handles can fail with EINVAL from
    dfs_sys_open. This was originally attributed to DFS_SYS_NO_LOCK, which was
    **wrong**: measured across mount flags (10 bursts x 16 threads each),

        sflags=0 (cache+lock on)   1/160 failures
        DFS_SYS_NO_CACHE          0/160
        NO_CACHE|NO_LOCK          0/160

    so locking does not prevent it. That makes sense -- dfs_sys's lock protects
    one handle's internal directory cache, and each thread here has its own
    handle, so the lock never sees the cross-handle race. The single failure
    landed on the very first burst, consistent with a cold root-directory-cache
    race, but one event is not enough to claim cache-on is worse.

    Either way the mitigation is serialising *creation*, not flag choice. The
    bulk writes afterwards are per-file and safe.
    """
    try:
        d = DfsSys(pool=POOL, cont=CONT)
        try:
            for tid in range(WORKERS):
                d.close_obj(d.open_rdwr_create(f"/ws_{tid}"))
        finally:
            d.close()
    except Exception as e:
        errs.append(f"precreate: {type(e).__name__}: {e}")


def writer(tid, errs):
    """Fill one file with distinct content, in CHUNK-sized appends."""
    try:
        d = DfsSys(pool=POOL, cont=CONT)
        try:
            buf = ctypes.create_string_buffer(
                bytes([(tid * 53 + 7) & 0xFF]) * CHUNK, CHUNK)
            obj = d.open_rdwr_create(f"/ws_{tid}")   # already exists now
            try:
                off = 0
                while off < FILE_BYTES:
                    d.write_obj_from(obj, off, CHUNK, buf)
                    off += CHUNK
            finally:
                d.close_obj(obj)
        finally:
            d.close()
    except Exception as e:
        errs.append(f"write t{tid}: {type(e).__name__}: {e}")


def reader(tid, nbytes, barrier, errs, moved):
    """Read the first nbytes of one file, sequentially, into a reused buffer.

    Records what it actually transferred in moved[tid]. A worker that dies on
    open transfers nothing, and the rate must not be credited with its share --
    see measure().
    """
    d = None
    try:
        d = DfsSys(pool=POOL, cont=CONT)
        buf = ctypes.create_string_buffer(CHUNK)
        buf[0] = 0
        buf[CHUNK - 1] = 0                 # pre-touch, untimed
        obj = d.open_rdonly(f"/ws_{tid}")
        try:
            barrier.wait()                 # ---- timing starts ----
            off = 0
            while off < nbytes:
                d.read_obj_into(obj, off, CHUNK, buf)
                off += CHUNK
            moved[tid] = off
        finally:
            d.close_obj(obj)
    except Exception as e:
        errs.append(f"read t{tid}: {type(e).__name__}: {e}")
        try:
            barrier.wait()
        except Exception:
            pass
    finally:
        if d is not None:
            d.close()


def measure(set_gb):
    per_worker = int(set_gb * 1e9 / WORKERS // CHUNK) * CHUNK
    if per_worker == 0:
        return None, per_worker, ["working set too small for one chunk"], 0
    # Discard any rep that lost a worker. Two reasons, and the second one bites
    # harder: the rate would be credited with bytes that worker never moved,
    # and the surviving workers face less contention -- so a partial rep is not
    # a sample of WORKERS-way throughput at all. Taking max() over reps then
    # selects precisely the most damaged one, which is how this harness came to
    # report 46 GB/s against a 15.8 GB/s-per-drive fabric.
    best, clean, allerr = 0.0, 0, []
    for _ in range(REPS):
        errs, moved = [], [0] * WORKERS
        barrier = threading.Barrier(WORKERS + 1)
        th = [threading.Thread(target=reader,
                               args=(k, per_worker, barrier, errs, moved))
              for k in range(WORKERS)]
        for x in th:
            x.start()
        barrier.wait()
        t0 = time.perf_counter()
        for x in th:
            x.join(timeout=900)
        dt = time.perf_counter() - t0
        if errs:
            allerr += errs
            continue
        want = per_worker * WORKERS
        if sum(moved) != want:            # short read without an exception
            allerr.append(f"short: moved {sum(moved)} of {want}")
            continue
        clean += 1
        best = max(best, sum(moved) / dt / 1e9)
    return (best if clean else None), per_worker, allerr, clean


if not SKIP_WRITE:
    print(f"writing {WORKERS} files x {FILE_BYTES/1e9:.1f} GB "
          f"= {TOTAL_BYTES/1e9:.1f} GB ...", flush=True)
    errs = []
    precreate(errs)
    if errs:
        print(f"  precreate failed: {errs[0]}", flush=True)
        sys.exit(1)
    t0 = time.perf_counter()
    th = [threading.Thread(target=writer, args=(k, errs))
          for k in range(WORKERS)]
    for x in th:
        x.start()
    for x in th:
        x.join()
    dt = time.perf_counter() - t0
    print(f"  wrote in {dt:.0f}s = {TOTAL_BYTES/dt/1e9:.2f} GB/s"
          f"{'  ERR ' + errs[0] if errs else ''}\n", flush=True)
    if errs:
        sys.exit(1)

print(f"read sweep: {WORKERS} threads, {CHUNK>>20} MiB chunks, best of {REPS}",
      flush=True)
print(f"{'working set':>13} {'per file':>10} {'GB/s':>8}   note", flush=True)
for s in SETS:
    if s * 1e9 > TOTAL_BYTES * 1.001:
        print(f"{s:>10.0f} GB {'':>10} {'skip':>8}   corpus is only "
              f"{TOTAL_BYTES/1e9:.0f} GB", flush=True)
        continue
    g, per, errs, clean = measure(s)
    note = f"{clean}/{REPS} clean"
    if errs:
        note += f", {len(errs)} discarded: {errs[0][:40]}"
    shown = "FAILED" if g is None else f"{g:8.2f}"
    print(f"{s:>10.0f} GB {per/1e9:>8.2f} GB {shown:>8}   {note}", flush=True)

print(f"\n비교 기준: 0.94 GB working set 에서 35.3 GB/s (bench_async_vs_sync, "
      f"서버 메모리 상주 가능) / Part C 2노드 end-to-end 집계 32.8 GB/s "
      f"(100 GB KV)", flush=True)
