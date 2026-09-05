"""GPU-direct round-trip gate through the Python binding (needs a GPU, torch, and
the GPU-direct DAOS client bundle -- see gpudirect/README.md "Phase 1").

    LMCACHE_DAOS_LIBDIR=/gds/lib64 D_MEM_DEVICE=1 python3 tests/test_gds_binding.py <pool> <cont> [MiB]

What it proves, layer by layer:
1. dfs_write_gpu / dfs_read_gpu work through ctypes with a caller-owned sgl and
   ``daos_mem_attr_t`` (device memory from torch, plain cudaMalloc pool);
2. a v2 object written as [host header page][GPU payload at 4096] reads back
   byte-exact into a fresh GPU buffer, and the header parses as committed;
3. the atomic publish (temp name -> dfs_move -> final name) leaves exactly one
   object behind;
4. timing of the GPU read vs. a host read of the same object (informational).
"""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import torch  # noqa: E402

from lmcache_daos import serde_v2 as v2  # noqa: E402
from lmcache_daos.dfs_binding import DfsSys, DFS_RDWR  # noqa: E402


def main():
    pool, cont = sys.argv[1], sys.argv[2]
    mib = int(sys.argv[3]) if len(sys.argv) > 3 else 40
    n = mib << 20
    dfs = DfsSys(pool=pool, cont=cont)
    assert dfs.gpu_supported(), "libdfs without dfs_read_gpu -- wrong bundle?"
    dev = torch.cuda.current_device()
    dfs.mkdir_p(v2.V2_PREFIX)

    key = f"gds-binding-test-{uuid.uuid4().hex}"
    final = v2.key_to_path(key)
    tmp = v2.temp_path(final, uuid.uuid4().hex[:8])
    meta = b"RemoteMetadata-stand-in\x00" * 2

    # deterministic payload on the GPU
    g = torch.Generator(device="cuda").manual_seed(1234)
    src = torch.randint(0, 256, (n,), dtype=torch.uint8, device="cuda", generator=g)
    torch.cuda.synchronize()

    # --- write: header page (host) + payload (GPU) to the temp name, then publish
    t0 = time.perf_counter()
    h = dfs.open_rdwr_create(tmp)
    try:
        page = v2.pack_header(meta, n)
        import ctypes
        buf = ctypes.create_string_buffer(page, len(page))
        dfs.write_obj_from(h, 0, len(page), buf)
        wrote = dfs.write_gpu_from(h, v2.payload_offset(), n, src.data_ptr(), dev)
    finally:
        dfs.close_obj(h)
    assert wrote == n, wrote
    parent = dfs.lookup(v2.V2_PREFIX, DFS_RDWR)
    try:
        dfs.move(parent, tmp.rsplit("/", 1)[1], parent, final.rsplit("/", 1)[1])
    finally:
        dfs.release(parent)
    t_write = time.perf_counter() - t0
    assert dfs.stat_size(tmp) is None, "temp name still present after move"
    assert dfs.stat_size(final) == v2.HEADER_SIZE + n, dfs.stat_size(final)

    # --- read back: header via host, payload via GPU
    dst = torch.zeros(n, dtype=torch.uint8, device="cuda")
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    h = dfs.open_rdonly(final)
    try:
        hdr = v2.parse_header(dfs.read_obj(h, 0, v2.HEADER_SIZE))
        assert hdr.committed and hdr.meta == meta and hdr.payload_len == n
        got = dfs.read_gpu_into(h, v2.payload_offset(), n, dst.data_ptr(), dev)
    finally:
        dfs.close_obj(h)
    t_read_gpu = time.perf_counter() - t0
    assert got == n, got
    torch.cuda.synchronize()
    assert torch.equal(src, dst), "GPU payload mismatch"

    # --- same object via the host path, for comparison
    hbuf = bytearray(n)
    hdst = (ctypes.c_char * n).from_buffer(hbuf)
    t0 = time.perf_counter()
    h = dfs.open_rdonly(final)
    try:
        got2 = dfs.read_obj_into(h, v2.payload_offset(), n, hdst)
    finally:
        dfs.close_obj(h)
    t_read_host = time.perf_counter() - t0
    assert got2 == n
    assert bytes(hbuf[:4096]) == src[:4096].cpu().numpy().tobytes()

    dfs.remove(final)
    print(f"GDS binding round-trip: {mib} MiB payload  write(gpu)={t_write*1e3:.1f} ms  "
          f"read(gpu)={t_read_gpu*1e3:.1f} ms ({n/t_read_gpu/1e9:.2f} GB/s)  "
          f"read(host)={t_read_host*1e3:.1f} ms ({n/t_read_host/1e9:.2f} GB/s)")
    print("RESULT: ALL OK")


if __name__ == "__main__":
    main()
