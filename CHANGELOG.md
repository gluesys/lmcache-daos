<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the
project intends [Semantic Versioning](https://semver.org/) from its first tag.

**Nothing has been released yet.** There are no tags, `pyproject.toml` still
says `0.0.1`, and no interface here carries a compatibility promise. What
follows is the state of `main`, written so that the first release notes have a
starting point.

## [Unreleased]

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

- No tagged release, no published wheel.
- No failure-mode testing: rank loss, network partition, pool exhaustion.
- No multi-tenant enforcement beyond the key namespace.
- No metrics export.
- Validated on one GPU and one client; 8-GPU and cross-node GPU measurements
  are missing, and the GPU-direct value proposition depends on them.
- The libfabric fix in `gpudirect/patches/` is not upstream yet
  (`doc/upstream/`), so the GPU-direct client stack is not shippable.
