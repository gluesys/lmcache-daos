# DAOS `theodore/b_cufile` draft: `dfs_write_gpu()` to a replicated object class fails over `ofi+verbs;ofi_rxm`

**Summary**: with the GPU-direct draft client (tip `c87080a70`, plus the build fixes in `gpudirect/patches/`) against a stock
2.9.100 server, `dfs_write_gpu()` from CUDA device memory works on `S16` (no replication) but fails on `RP_2G1`, `RP_2G4`
and `RP_3G1` containers as soon as the write needs a bulk transfer (>= 64 KiB). Reads (`dfs_read_gpu`) from the same
replicated containers and host-memory writes to them are fine. Over `ucx+rc_v` the same GPU-source replicated writes succeed.

**Server-side evidence** (rank 0 engine, `RPC/HG/BULK/OBJECT/DTX=DEBUG`), four update RPCs received by xstream 5 at
12:24:28.331, each starting `obj_bulk_transfer() bulk_op 105` (GET); 14 s later all four complete with:

```
mercury->hg [error] ... HG_IO_ERROR
crt_hg_bulk_transfer_cb() crt_hg_bulk_transfer_cb,hg_cbinfo->ret: 22 (HG_IO_ERROR)
obj_bulk_comp_cb() bulk transfer failed: -1020
crt_hg_bulk_transfer_cb() bulk_cbinfo->bci_verify_cb failed, rc: -1020.
```

The client then gets an error reply, retries, and the next attempt stalls another 15 s (client log gaps of 14.0/15.0 s
repeating). No HCA counter increments on either side (`local_ack_timeout_err`, `req_remote_access_errors` unchanged), no
libfabric warnings on the client. The 14 s matches transport retry exhaustion for an RDMA READ that never completes.

**What differs from the working cases**
- Same process, same client memory registration path (`fi_mr_regattr` with `FI_HMEM_CUDA`, dma-buf), same servers: S16 bulk
  GETs from GPU memory by all 16 targets succeed (4 KiB–32 MiB, 3/3 round-trips; 8 GiB sweeps).
- Host-source writes to the same RP_2G4 container succeed (8 GiB, 4 workers).
- 4 KiB writes (inline, no bulk) to RP_2 succeed.

**Candidate mechanisms** (not confirmed): (a) the replicated update path re-creates or forwards the client bulk handle in a
way that drops the HMEM attributes (device iface/id) the draft attaches via `daos_mem_attr_t`, so the follower's rdma_read
targets an rkey/iova pair the NIC cannot serve; (b) the follower opens a fresh rxm connection to the client and the first RMA
on it against a CUDA MR is mishandled. Distinguishing them needs `FI_LOG_LEVEL=debug` on the server side for the failing
xstream, or a run where the follower already has a connection to the client.

**Reproduction**: `tests/dfs_gpu_rt.c` (in `exastor/lmcache-daos`) against a `--file-oclass=RP_2G1 --chunk-size=4194304`
container with `D_MEM_DEVICE=1`, CUDA-enabled libfabric (see `libfabric-0001-verbs-close-dmabuf-fd.patch` and the CUDA
dmabuf routing present in libfabric main), agent domain `mlx5_0`. Expected: `RESULT: ALL OK` (as on S16). Actual: 4096 OK,
then 64 KiB write returns `DER_HG(-1020)` after ~15 s × retries.
