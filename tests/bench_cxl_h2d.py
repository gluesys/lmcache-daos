# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Can CXL-backed host memory serve as a KV staging tier? H2D and read bandwidth by NUMA node.

client-6 has a 128 GiB CXL memory expander onlined as `system-ram` on NUMA node
2 (0 CPUs). Node distances put it *closer* than the remote DRAM socket:

    node       0    1    2
       0:     10   21   14      <- GPU (0000:5a:00.0) and the NIC are on node 0

A "CPU cache" tier on CXL would be attractive: 128 GiB is enough to hold the
100 GB working set the DRAM tier was declared impractical for. But that tier's
cost is host->device copy, so two things decide it:

  1. can pinned memory be placed on CXL at all, and
  2. what is CXL read bandwidth relative to DRAM?

Both are measured rather than assumed, and the harness is built to avoid three
traps that produced wrong numbers on the first attempt:

  * **CUDA must be initialised before the memory policy is set.** Binding first
    makes the driver's own allocations come from the target node, which fails
    outright on the CXL node (cudaErrorDevicesUnavailable) -- a harness artefact,
    not a property of CXL.
  * **Placement must be forced and verified.** torch.empty does not fault pages
    in; touching only the first and last byte leaves the buffer unresident, so a
    meminfo delta shows nothing and the "which node am I measuring" question is
    unanswered. Every page is written, then the delta is checked.
  * **The copy destination must be placed deliberately.** With the policy still
    bound, a scratch destination also lands on the target node, so a "CXL read"
    measurement silently becomes CXL->CXL.

Two pinning paths are tried, because they are not equivalent:
``torch pin_memory`` (cudaHostAlloc -- the driver chooses the pages) and
``cudaHostRegister`` on pages we placed ourselves, which is what an
implementation wanting NUMA control would actually use.

    python3 /lmd/tests/bench_cxl_h2d.py             # nodes 0,1,2 in turn
    NODE=2 python3 /lmd/tests/bench_cxl_h2d.py      # one node, in-process
"""

import ctypes
import os
import subprocess
import sys
import time

SZ = int(os.environ.get("SZ", 1 << 30))          # 1 GiB per buffer
ITERS = int(os.environ.get("ITERS", 5))
NODES = [int(x) for x in os.environ.get("NODES", "0,1,2").split(",")]
PAGE = 4096

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_SYS_set_mempolicy = 238                          # x86_64
MPOL_BIND, MPOL_DEFAULT = 2, 0


def set_membind(node):
    """set_mempolicy(MPOL_BIND, {node}); node=None restores the default."""
    if node is None:
        rc = _libc.syscall(ctypes.c_long(_SYS_set_mempolicy),
                           ctypes.c_int(MPOL_DEFAULT), None, ctypes.c_ulong(0))
    else:
        mask = ctypes.c_ulong(1 << node)
        rc = _libc.syscall(ctypes.c_long(_SYS_set_mempolicy),
                           ctypes.c_int(MPOL_BIND),
                           ctypes.byref(mask), ctypes.c_ulong(64))
    if rc != 0:
        raise OSError(ctypes.get_errno(), f"set_mempolicy({node}) failed")


def node_used_kb(node):
    try:
        with open(f"/sys/devices/system/node/node{node}/meminfo") as f:
            for ln in f:
                if "MemUsed:" in ln:
                    return int(ln.split()[-2])
    except Exception:
        return -1
    return -1


def fault_in(t):
    """Write one byte per page so the whole buffer is resident under the
    current policy -- torch.empty alone leaves it unfaulted."""
    mv = memoryview(t.numpy().data).cast("B") if hasattr(t, "numpy") else None
    n = t.numel()
    for off in range(0, n, PAGE):
        t[off] = 1
    t[n - 1] = 1


def run_one(node):
    import torch
    assert torch.cuda.is_available(), "no CUDA device"
    dev = torch.device("cuda:0")
    cudart = ctypes.CDLL("libcudart.so")
    cudart.cudaHostRegister.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                        ctypes.c_uint]
    cudart.cudaHostRegister.restype = ctypes.c_int
    cudart.cudaHostUnregister.argtypes = [ctypes.c_void_p]
    cudart.cudaHostUnregister.restype = ctypes.c_int

    # --- CUDA first, then policy (see module docstring) -------------------
    gpu = torch.empty(SZ, dtype=torch.uint8, device=dev)
    torch.cuda.synchronize()

    # destination for the host-read test, deliberately on node 0 (local DRAM)
    set_membind(0)
    dst = torch.empty(SZ, dtype=torch.uint8)
    fault_in(dst)

    # --- buffer under test ------------------------------------------------
    set_membind(node)
    before = node_used_kb(node)
    src = torch.empty(SZ, dtype=torch.uint8)
    fault_in(src)
    landed = (node_used_kb(node) - before) * 1024
    placed = landed > SZ * 0.8

    set_membind(None)

    def h2d(t, n=ITERS, nb=False):
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        for _ in range(n):
            gpu.copy_(t, non_blocking=nb)
        torch.cuda.synchronize()
        return SZ * n / (time.perf_counter() - t0) / 1e9

    def host_read(n=ITERS):
        dst.copy_(src)                                  # warm
        t0 = time.perf_counter()
        for _ in range(n):
            dst.copy_(src)
        return SZ * n / (time.perf_counter() - t0) / 1e9

    # Order matters. Measure the un-pinned buffer FIRST, while it is still
    # certainly on the target node; pinning may relocate it (see below), and a
    # read measured afterwards would describe wherever it ended up.
    rd = host_read()
    plain = h2d(src)

    # cudaHostRegister on pages we placed ourselves. Watching node MemUsed
    # across the call is the point: pinning is incompatible with ZONE_MOVABLE,
    # so the kernel may migrate the pages out rather than fail, which would
    # make a "CXL pinned" number secretly a DRAM number.
    used_before_reg = node_used_kb(node)
    rc_reg = cudart.cudaHostRegister(ctypes.c_void_p(src.data_ptr()),
                                     ctypes.c_size_t(SZ), 0)
    used_after_reg = node_used_kb(node)
    migrated = (used_before_reg - used_after_reg) * 1024
    reg = h2d(src) if rc_reg == 0 else -1.0
    rd_reg = host_read() if rc_reg == 0 else -1.0
    if rc_reg == 0:
        cudart.cudaHostUnregister(ctypes.c_void_p(src.data_ptr()))

    # cudaHostAlloc path -- driver picks the pages, we only set the policy.
    set_membind(node)
    pin_alloc_err, host_p = None, None
    try:
        host_p = torch.empty(SZ, dtype=torch.uint8, pin_memory=True)
        fault_in(host_p)
    except Exception as e:
        pin_alloc_err = f"{type(e).__name__}:{str(e).splitlines()[0][:48]}"
    set_membind(None)
    alloc = h2d(host_p, nb=True) if host_p is not None else -1.0

    print(f"RESULT node={node} placed={'yes' if placed else 'NO'} "
          f"landed={landed/1e9:.2f} host_read={rd:.2f} h2d_plain={plain:.2f} "
          f"h2d_reg={reg:.2f} host_read_after_reg={rd_reg:.2f} "
          f"migrated={migrated/1e9:.2f} h2d_hostalloc={alloc:.2f} rc_reg={rc_reg} "
          f"alloc_err={'-' if pin_alloc_err is None else pin_alloc_err.replace(' ','_')}",
          flush=True)


if os.environ.get("NODE"):
    run_one(int(os.environ["NODE"]))
    sys.exit(0)

print(f"buffer {SZ/1e9:.2f} GB, {ITERS} iters, dst pinned to node 0\n", flush=True)
print(f"{'node':>5} {'placed':>7} {'host read':>10} {'H2D reg':>9} "
      f"{'read after reg':>15} {'migrated':>9} {'H2D hostalloc':>14}   note",
      flush=True)
for n in NODES:
    out = subprocess.run([sys.executable, __file__],
                         env=dict(os.environ, NODE=str(n)),
                         capture_output=True, text=True)
    ln = [l for l in out.stdout.splitlines() if l.startswith("RESULT")]
    if not ln:
        print(f"{n:>5}  FAILED: {(out.stderr or out.stdout).strip()[-110:]}",
              flush=True)
        continue
    f = dict(kv.split("=", 1) for kv in ln[0].split()[1:])
    def g(k):
        v = float(f[k])
        return "     실패" if v < 0 else f"{v:>8.2f}"
    note = []
    if f["rc_reg"] != "0":
        note.append(f"cudaHostRegister rc={f['rc_reg']}")
    if f["alloc_err"] != "-":
        note.append(f"pin_memory {f['alloc_err'].replace('_',' ')}")
    print(f"{n:>5} {f['placed']:>7} {g('host_read')} {g('h2d_reg')} "
          f"{g('host_read_after_reg'):>15} {float(f['migrated']):>8.2f}G "
          f"{g('h2d_hostalloc'):>14}   {'; '.join(note)}", flush=True)
