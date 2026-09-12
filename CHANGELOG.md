<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the
project intends [Semantic Versioning](https://semver.org/) from its first tag.

No interface here carries a compatibility promise yet; the leading `0.`
is doing real work.

## [Unreleased]

### Added

- `nixl/` — a DAOS backend plugin for
  [NIXL](https://github.com/ai-dynamo/nixl). NIXL ships 16 backends and none of
  them speaks DAOS. Registration and transfer both work against a live pool;
  the two programs in `nixl/tests/` pass. It has not been run under a NIXL
  agent yet, only driven directly, and it has not been measured — the
  development host is 1 GbE with no RDMA.

  It is built on the raw object API rather than DFS, for the reason the
  layerwise measurement turned up: DFS pays ~0.63 ms of fixed cost per object
  against 0.0137 ms for dkey/akey, and the object API can fold a whole
  descriptor list into one `daos_obj_fetch()` through its iod array, which is
  the shape `prepXfer()` hands us. Offsets are cut at a 64 MiB span into
  dkey and akey so that descriptors in one span fold into a single RPC while
  separate spans still spread across targets.

  Not yet: `VRAM_SEG` (the host has no `nvidia_fs`, so the GPU-direct entry
  points cannot be exercised), a thread pool (one thread per posted request),
  and any performance number.

### Changed

- The libfabric `prov/verbs` dma-buf fd fix is prepared for upstream submission
  against `main` and is no longer a sketch: it releases the descriptor with
  `ofi_hmem_put_dmabuf_fd()` rather than `close()`, because that helper
  dispatches per HMEM interface and ROCR must go through
  `hsa_amd_portable_close_dmabuf()`. verbs turns out to be the only in-tree
  caller of `ofi_hmem_get_dmabuf_fd()` that never releases the fd. See
  `doc/upstream/`.
- The patch kept in `gpudirect/patches/` still uses `close()` and is unchanged.
  `ofi_hmem_put_dmabuf_fd()` was added after v1.22.0, which is the libfabric
  DAOS bundles, so the helper does not exist in that tree. The patch header now
  says so.

### Fixed

- Corrected the bundled libfabric version throughout the docs and patch headers.
  It was written as 1.25, a release that does not exist. DAOS
  `utils/build.config` pins `ofi=v1.22.0`.

### Deferred

- The LMCache async-serializer proposal is on hold with the reasoning recorded
  in `doc/upstream/lmcache-async-serializer-option.md`. The in-process path it
  targets is not deprecated, but the only measured benefit comes from the
  experimental GPU-direct backend.

## [0.1.0] - 2026-09-09

First tagged release. It marks the point where the repository became something
another team could pick up -- licensed, CI-checked, with the maturity of each
backend stated -- rather than any change in the code's capability.

### Backends

- **in-process connector** (`lmcache_daos.connector.DaosConnector`) — LMCache
  `RemoteConnector` plugin reached through `plugin://daos/<pool>/<container>`.
  KV chunks are one DFS file each, self-describing (`[8 B prefix][meta][payload]`),
  read zero-copy into the `MemoryObj` buffer. Batched get/put. Runs on a stock
  DAOS client. *Since 2026-07.*
- **MP mode** (`lmcache_daos.mp`) — DAOS as an `L2AdapterInterface` behind
  LMCache's multiprocess cache server, so several vLLM instances on a node share
  one pinned L1 backed by DAOS. Separate task and I/O thread pools, eventfd
  completions, size-verified lookup, idempotent store. Also stock client.
  *Since 2026-09-04.*
- **GPU-direct backend** (`lmcache_daos.gds_backend.DaosGdsBackend`) —
  ⚠️ **experimental, not for production.** Reads DAOS straight into GPU memory
  with `dfs_read_gpu`/`dfs_write_gpu`, taking host DRAM off the data path. Needs
  an unmerged DAOS draft branch and seven patches, has a known failure writing
  from GPU memory to replicated containers over verbs, and is validated on a
  single GPU only. See the README section "모드별 성숙도". *Since 2026-09-06.*

### On-disk formats

- **v1** (`serde.py`) — `[8 B prefix][meta][payload]`, used by the in-process
  connector. Truncated objects are reported as a miss rather than an error.
- **v2** (`serde_v2.py`) — `[4 KiB header page][payload at 4096]` with a
  committed-state flag and header CRC, published atomically by rename. Used by
  the GPU-direct backend, whose payload must start page-aligned. Namespaced
  under `/v2/` so both formats can share a container. Carries no compatibility
  promise while the backend is experimental.

### Operational findings that shaped the code

Kept here because each one changed a default:

- The recommended transport is `ofi+verbs;ofi_rxm`, not `ucx+rc_v`. An earlier
  verdict blaming libfabric for silent read corruption was **retracted**: the
  cause was two DAOS ranks formatting the same dual-port drives. Verbs passes
  every integrity test once the drives are disjoint.
- Adapters connect to every target at start-up (an SX-class probe). Without it,
  a first RPC issued during a store could lose its rdma_cm RTU on a lossy fabric
  and stall roughly 15 s over UCX.
- Concurrency, not tuning knobs, controls the p95 tail when L1 is smaller than
  the working set: the queueing is bandwidth saturation.

### Project

- Apache-2.0 (`LICENSE`, `NOTICE`), SPDX headers on all sources.
- CI: unit tests, syntax, and license-header checks on both GitLab and the
  GitHub mirror. Integration tests need a DAOS cluster and stay manual.
- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`.
- Internal IPv4 addresses in the tree are documentation-range placeholders
  (RFC 5737 / RFC 2544); host suffixes are preserved.

### Not done

- Not published to any package index; the wheel is built from the tag.
- No failure-mode testing: rank loss, network partition, pool exhaustion.
- No multi-tenant enforcement beyond the key namespace.
- No metrics export.
- Validated on one GPU and one client; 8-GPU and cross-node GPU measurements
  are missing, and the GPU-direct value proposition depends on them.
- The libfabric fix in `gpudirect/patches/` is not upstream yet
  (`doc/upstream/`), so the GPU-direct client stack is not shippable.
