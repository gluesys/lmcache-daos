<!-- Body text for the libfabric pull request. Not part of the package. -->

`vrb_reg_hmem_dmabuf()` takes a dma-buf fd from `ofi_hmem_get_dmabuf_fd()`, passes it to `ibv_reg_dmabuf_mr()`, and never releases it. The MR holds its own reference to the dma-buf, so the fd is only needed for the duration of the registration call; as it stands, every registration leaks one descriptor.

`ofi_hmem_put_dmabuf_fd()` was added for exactly this in #10716. cxi, opx and efa all use it — efa calls it immediately after `ibv_reg_dmabuf_mr()`, which is the same shape as this change. verbs is the only in-tree caller of `ofi_hmem_get_dmabuf_fd()` that never releases the fd. (#11087 fixed the same class of leak in fabtests.)

A bare `close()` would not be correct here: `rocr_hmem_put_dmabuf_fd()` releases through `hsa_amd_portable_close_dmabuf()`.

### Why this is more than untidiness

The leak is unbounded when the MR cache is disabled. Mercury's `na_ofi` sets `FI_MR_CACHE_MAX_COUNT=0`, so a DAOS client reading into GPU memory registers anew on every operation. Reading 8 GiB in 4 MiB chunks exhausts `nofile=1024`, after which `cuMemGetHandleForAddressRange()` fails with `CUDA_ERROR_OPERATING_SYSTEM` and the transport goes down mid-run.

Measured on H100 + ConnectX-7 over `verbs;ofi_rxm` on a dmabuf-only platform (NVIDIA open kernel module, no peer-memory client), DAOS 2.9 with Mercury 2.4.1:

| | open dma-buf fds after 1.5 s of I/O |
|---|---|
| before | 634 |
| after | 1 |

That run used a direct `close(fd)`. `cuda_put_dmabuf_fd()` is precisely that `close()`, so the measurement applies to this patch unchanged. I have no ROCR or ZE hardware to test on.

### errno

`errno` is saved and restored across the release call, because the failover path below reports the errno from `ibv_reg_dmabuf_mr()`.

### Left alone deliberately

`ze` and `synapseai` map to `ofi_hmem_no_put_dmabuf_fd()`, so their fds are not released anywhere either. `ze_hmem_get_dmabuf_fd()` does hand back an exported fd, so that looks like a separate gap in `hmem_ze`; I did not widen this fix to cover it.
