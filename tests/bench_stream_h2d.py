# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""P4 gate: does completion-ordered streaming actually buy the overlap?

The whole upstream RFC rests on one claim: if a backend yields chunks as they
complete, the engine can copy chunk i to the GPU while chunk i+1 is still being
read, turning read+H2D from a sum into a max. Our own decomposition predicted
1.4-1.6x from that. This measures it end to end with real DAOS reads into pinned
host memory and real CUDA H2D copies.

  arm BATCH   read all N chunks, then H2D all N        (what LMCache does today)
  arm STREAM  H2D each chunk the moment it lands       (what stream_get enables)

Also reports read-only and H2D-only times so the speedup can be checked against
max(read, h2d) / (read + h2d) rather than taken on faith.

Needs the GPU mostly free, so run it in a one-off container, not inside the
vLLM one (vLLM holds 90% of device memory).

    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=streamtest \
        N=64 CHUNK_MB=32 WORKERS=16 python3 /lmd/tests/bench_stream_h2d.py
"""

import asyncio
import concurrent.futures
import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, "/lmd")

import torch

from lmcache_daos.dfs_binding import DfsSys
from lmcache_daos.streaming import stream_completions

POOL = os.environ["DAOS_TEST_POOL"]
CONT = os.environ["DAOS_TEST_CONT"]
N = int(os.environ.get("N", 64))
CHUNK = int(os.environ.get("CHUNK_MB", 32)) * 1024 * 1024
WORKERS = int(os.environ.get("WORKERS", 16))
INFLIGHT = int(os.environ.get("INFLIGHT", WORKERS))
REPS = int(os.environ.get("REPS", 3))
GPU_SLOTS = int(os.environ.get("GPU_SLOTS", 8))
TOTAL = N * CHUNK

assert torch.cuda.is_available(), "no CUDA device visible"
dev = torch.device("cuda:0")

setup = DfsSys(pool=POOL, cont=CONT)
_tls = threading.local()
handles = []
hlock = threading.Lock()


def handle():
    h = getattr(_tls, "h", None)
    if h is None:
        h = DfsSys(pool=POOL, cont=CONT)
        _tls.h = h
        _tls.objs = {}
        with hlock:
            handles.append(h)
    return h


def read_chunk(idx):
    """Blocking DAOS read of chunk `idx` straight into its pinned host buffer."""
    h = handle()
    obj = _tls.objs.get(idx)
    if obj is None:
        obj = h.open_rdonly(f"/st_{idx}")
        _tls.objs[idx] = obj
    h.read_obj_into(obj, 0, CHUNK, dests[idx])
    return idx


try:
    # ---- corpus (create serially: concurrent create on NO_LOCK handles EINVALs)
    print(f"corpus {N} x {CHUNK>>20} MiB = {TOTAL/1e9:.2f} GB", flush=True)
    src = ctypes.create_string_buffer(bytes([0x5A]) * CHUNK, CHUNK)
    for i in range(N):
        o = setup.open_rdwr_create(f"/st_{i}")
        try:
            setup.write_obj_from(o, 0, CHUNK, src)
        finally:
            setup.close_obj(o)
    print("  written", flush=True)

    # ---- pinned host buffers (one per chunk: arm BATCH needs them all) ----
    host = [torch.empty(CHUNK, dtype=torch.uint8, pin_memory=True)
            for _ in range(N)]
    dests = [(ctypes.c_char * CHUNK).from_address(t.data_ptr()) for t in host]
    gpu = [torch.empty(CHUNK, dtype=torch.uint8, device=dev)
           for _ in range(GPU_SLOTS)]
    stream = torch.cuda.Stream(device=dev)
    torch.cuda.synchronize()
    print(f"  pinned {TOTAL/1e9:.2f} GB host, {GPU_SLOTS*CHUNK/1e9:.2f} GB device",
          flush=True)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS,
                                                 thread_name_prefix="rd")
    # warm the per-thread handles/opens so setup cost is not in any arm
    list(pool.map(read_chunk, range(N)))
    torch.cuda.synchronize()

    def h2d(idx):
        with torch.cuda.stream(stream):
            gpu[idx % GPU_SLOTS].copy_(host[idx], non_blocking=True)

    def arm_read_only():
        t0 = time.perf_counter()
        list(pool.map(read_chunk, range(N)))
        return time.perf_counter() - t0

    def arm_h2d_only():
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        for i in range(N):
            h2d(i)
        stream.synchronize()
        return time.perf_counter() - t0

    def arm_batch():
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        list(pool.map(read_chunk, range(N)))     # read ALL first
        for i in range(N):
            h2d(i)
        stream.synchronize()
        return time.perf_counter() - t0

    async def _stream():
        async for idx, res in stream_completions(
                asyncio.get_running_loop(), pool, read_chunk, range(N),
                INFLIGHT):
            if isinstance(res, BaseException):
                raise res
            h2d(idx)                              # H2D the instant it lands

    def arm_stream():
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        asyncio.run(_stream())
        stream.synchronize()
        return time.perf_counter() - t0

    best = {}
    for name, fn in (("read-only", arm_read_only), ("h2d-only", arm_h2d_only),
                     ("BATCH", arm_batch), ("STREAM", arm_stream)):
        b = min(fn() for _ in range(REPS))
        best[name] = b
        print(f"{name:>10}: {b*1000:7.1f} ms  = {TOTAL/b/1e9:6.2f} GB/s",
              flush=True)

    r, h = best["read-only"], best["h2d-only"]
    print(f"\nserial 예측  read+h2d = {(r+h)*1000:.1f} ms "
          f"({TOTAL/(r+h)/1e9:.2f} GB/s)", flush=True)
    print(f"overlap 예측 max(read,h2d) = {max(r,h)*1000:.1f} ms "
          f"({TOTAL/max(r,h)/1e9:.2f} GB/s) -> 이론 {(r+h)/max(r,h):.2f}x",
          flush=True)
    print(f"실측 STREAM / BATCH = {best['BATCH']/best['STREAM']:.2f}x",
          flush=True)
finally:
    try:
        pool.shutdown(wait=True)
    except Exception:
        pass
    for h_ in handles:
        try:
            h_.close()
        except Exception:
            pass
    for i in range(N):
        try:
            setup.remove(f"/st_{i}")
        except Exception:
            pass
    setup.close()
