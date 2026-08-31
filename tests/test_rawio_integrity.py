"""Byte-level integrity of the DFS read/write path under concurrency.

Written to localise a real failure: with the DAOS remote backend, vLLM restores
KV that is wrong AND different on every retrieve of the same key, while the
identical test against LMCache's LocalCPUBackend is correct and deterministic.
That puts the fault somewhere in the DAOS path, but "the DAOS path" spans the
DFS binding, the aliased-buffer zero-copy trick in the connector, and the
LMCache MemoryObj integration.

This test exercises only the bottom layer -- write_obj_from / read_obj_into on
aliased ctypes buffers, at the sizes and thread counts the connector really
uses. If bytes come back wrong here, the binding is at fault. If they come back
clean, the binding is exonerated and the fault is above it.

Two things are deliberately mirrored from the connector rather than simplified,
because they are the parts most likely to be wrong:
  - the buffer handed to DAOS *aliases* a Python buffer via
    (c_char * n).from_buffer(...) instead of being a private copy
  - header and payload are written as two offset writes, and the payload is
    read back from a non-zero offset, so any off-by-one in offset handling
    shows up as a shifted comparison rather than a clean failure

Each thread uses a distinct byte pattern keyed to its own id, so a cross-thread
buffer mix-up is reported as such instead of looking like random corruption.

The payload offset is a parameter because it is the thing under suspicion.
Corruption was first seen with the connector's real layout, where a 36-byte
header pushes the payload to file offset 36 and every 4 MiB DFS chunk boundary
therefore falls in the middle of the transfer. Running the same size and thread
count at offset 0 and at a chunk-aligned offset distinguishes two hypotheses
that call for completely different fixes:

  - straddling is the cause      -> aligned offsets pass, and aligning the
                                    payload in the on-disk format fixes it
  - concurrent multi-chunk I/O
    is broken regardless         -> aligned offsets also fail, and no format
                                    change helps

    DAOS_TEST_POOL=gdspool DAOS_TEST_CONT=kvlmc \\
        python3 tests/test_rawio_integrity.py \\
            [chunk_MiB] [threads] [rounds] [payload_offset] [mode]

payload_offset accepts a byte count, or "hdr" for the connector's real 36-byte
layout, or "chunk" for one full 4 MiB chunk.

mode is loop (default) or burst, and it matters more than any other parameter.
In loop mode each thread interleaves its own write and read, so concurrent
READS are sparse -- that is why this test reported PASS 80/80 on the same 28
MiB and 16 threads where the LMCache-level reproducer failed, since
batched_get releases sixteen reads at once. burst holds every thread after its
write and releases the reads together, reproducing that density with no
LMCache and no Python object as the destination. A loop-mode pass is therefore
not evidence of absence.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lmcache_daos import serde  # noqa: E402
from lmcache_daos.dfs_binding import DfsSys  # noqa: E402

POOL = os.environ.get("DAOS_TEST_POOL", "gdspool")
CONT = os.environ.get("DAOS_TEST_CONT", "kvlmc")


def pattern(tid: int, n: int) -> bytes:
    """Per-thread, position-dependent bytes.

    Position dependence catches a shifted/offset read; the thread id catches a
    read that landed in another thread's object.
    """
    base = bytes(((i * 31 + tid * 101) & 0xFF) for i in range(4096))
    reps = n // len(base) + 1
    return (base * reps)[:n]


DFS_CHUNK = 4 * 1024 * 1024


def main() -> int:
    chunk = int(sys.argv[1]) * 1024 * 1024 if len(sys.argv) > 1 else 28 * 1024 * 1024
    nthr = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    off_arg = sys.argv[4] if len(sys.argv) > 4 else "hdr"
    mode = sys.argv[5] if len(sys.argv) > 5 else "loop"
    if mode not in ("loop", "burst"):
        print('mode: loop|burst'); return 2

    meta = b"m" * 28
    hdr_len = serde.prefix_size() + len(meta)          # the connector's 36 B
    if off_arg == "hdr":
        payload_off = hdr_len
    elif off_arg == "chunk":
        payload_off = DFS_CHUNK
    else:
        payload_off = int(off_arg)
    if payload_off < hdr_len and payload_off != 0:
        print(f"payload_offset {payload_off} would overlap the {hdr_len} B "
              f"header; use 0 to skip the header write")
        return 2

    dfs = DfsSys(pool=POOL, cont=CONT)
    print(f"pool={POOL} cont={CONT} chunk={chunk >> 20}MiB threads={nthr} "
          f"rounds={rounds} payload_off={payload_off} "
          f"({'chunk-aligned' if payload_off % DFS_CHUNK == 0 else 'straddling'}) "
          f"mode={mode}")

    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(nthr) if mode == "burst" else None

    def worker(tid: int) -> None:
        want = pattern(tid, chunk)
        # Alias, do not copy -- this is what the connector does.
        src_buf = bytearray(want)
        src = (ctypes.c_char * chunk).from_buffer(src_buf)
        header = serde.prefix_pack(len(meta), chunk) + meta
        path = f"/rawio_o{payload_off}_t{tid}"

        for r in range(rounds):
            obj = dfs.open_rdwr_create(path)
            try:
                if payload_off:
                    hdr = (ctypes.c_char * len(header)).from_buffer_copy(header)
                    dfs.write_obj_from(obj, 0, len(header), hdr)
                dfs.write_obj_from(obj, payload_off, chunk, src)
            finally:
                dfs.close_obj(obj)

            if barrier is not None:
                # Burst mode. Without this each thread interleaves its own
                # write and read, so concurrent READS are sparse -- which is
                # why this test passed 80/80 while the LMCache-level
                # reproducer, whose batched_get releases 16 reads at once,
                # failed on the same sizes and thread count. Holding every
                # thread here until all writes are done and then releasing
                # the reads together reproduces that density without LMCache
                # or any Python object in the destination.
                barrier.wait()

            dst_buf = bytearray(chunk)
            dst = (ctypes.c_char * chunk).from_buffer(dst_buf)
            obj = dfs.open_rdonly(path)
            try:
                got = dfs.read_obj_into(obj, payload_off, chunk, dst)
            finally:
                dfs.close_obj(obj)

            if got != chunk:
                with lock:
                    errors.append(f"t{tid} r{r}: short read {got} != {chunk}")
                continue
            if bytes(dst_buf) == want:
                continue

            # Diagnose rather than just failing: where, and whose data is it?
            first = next((i for i in range(chunk) if dst_buf[i] != want[i]), -1)
            owner = "unknown"
            for other in range(nthr):
                if bytes(dst_buf[:4096]) == pattern(other, 4096):
                    owner = f"thread {other}'s pattern"
                    break
            bad = sum(1 for i in range(0, chunk, 4096) if dst_buf[i] != want[i])
            with lock:
                errors.append(
                    f"t{tid} r{r}: MISMATCH at byte {first}, "
                    f"{bad}/{chunk // 4096} sampled pages differ, "
                    f"head looks like {owner}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(nthr)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        print(f"FAIL: {len(errors)} problem(s)")
        for e in errors[:12]:
            print("  " + e)
        return 1
    print(f"PASS: {nthr * rounds} write/read cycles, all bytes identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
