<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the
project intends [Semantic Versioning](https://semver.org/) from its first tag.

No interface here carries a compatibility promise yet; the leading `0.`
is doing real work.

## [Unreleased]

### Changed

- `doc/upstream/` records what actually happened to the submissions rather than
  what was prepared. The libfabric fix is [PR #12828]; the NIXL null-deref is
  [issue #2245] and [PR #2246]; the LMCache double-unpin is [issue #5090].

  The stored `.patch` is now regenerated from the upstream commit with
  `git format-patch` instead of being edited by hand, so it matches what was
  submitted byte for byte. The author address on it changed to
  `hgichon@gmail.com`, because GitHub only marks a signature Verified when the
  commit author email is a verified address on the signing account -- a
  registered key alone is not enough.

  Also written down: what each upstream actually requires. libfabric and DAOS
  want DCO and nothing else (DAOS additionally routes review by Jira ticket);
  NVIDIA repositories require GPG-signed commits and then, separately, decline
  external contributors until a maintainer comments `/build`. That second gate
  is not something a contributor can satisfy, which is worth knowing before
  assuming a red check needs fixing.

[PR #12828]: https://github.com/ofiwg/libfabric/pull/12828
[issue #2245]: https://github.com/ai-dynamo/nixl/issues/2245
[PR #2246]: https://github.com/ai-dynamo/nixl/pull/2246
[issue #5090]: https://github.com/LMCache/LMCache/issues/5090

### Changed

- The MP-mode L2 adapter can now say it is broken. It already counted work --
  seventeen counters and a periodic status thread -- but nothing in that told
  an outage from an idle node: the adapter turns every DAOS error into a cache
  miss, so when the backend goes away the task counters simply stop advancing,
  which is exactly what an unused node looks like. Three fields close that:
  `last_ok`, `last_error` with its timestamp, and `errors_by_op` splitting what
  `errors` lumped together. `report_status()` derives `healthy` and
  `since_last_success_s` from them.

  The status thread also stopped being silent at the worst moment. It only
  logged when task counts changed, which during an outage is never; it now
  reports on every tick while unhealthy and calls out each transition at
  WARNING. And it runs by default: `status_interval_s` was 0, so a default
  deployment started no status thread and reported nothing at all. It is now
  60 s.

  This is MP-only and stays in our tree because upstream has nowhere to put it.
  LMCache's health machinery hangs entirely off `RemoteConnector` --
  `health_monitor/checks/` holds one file, for remote backends -- while
  `L2AdapterInterface` has eleven abstract methods and not one of them asks
  whether the backend is alive. Proposing that contract upstream is the longer
  fix; this is what can be done without waiting for it.

### Added

- The in-process connector answers LMCache's health probe: `support_ping()` and
  `ping()`. This is less a new feature than switching one on. LMCache's
  `RemoteBackendHealthCheck` opens with

      # If connector doesn't support ping, assume it's healthy
      if not connector.support_ping():
          return True

  so until now the backend reported healthy no matter what DAOS was doing. That
  matters here more than for most connectors, because every DAOS error in
  `connector.py` is caught and turned into a cache miss — right for a cache,
  but it makes an outage and a cold cache look identical from outside: the hit
  rate falls and nothing else changes.

  Answering it turns on machinery that already existed upstream — periodic
  probing, reconnect attempts, `update_remote_ping_latency`,
  `update_remote_ping_error_code`, the `lmcache_is_healthy` gauge and the
  fallback policy. The DAOS `rc` is passed through rather than flattened, so
  the recorded code distinguishes a missing container from an unreachable
  network.

  The probe carries its own 3 s timeout (`DAOS_PING_TIMEOUT`), under LMCache's
  own 5 s so that whoever gives up first is also the one that can stop waiting
  on the thread. Without it a probe against a stopped server ran past three
  minutes, well beyond the 30 s poll interval. It runs on a dedicated
  single-thread pool: a stuck probe must not take capacity from the data path
  it exists to report on, and one thread bounds that to one.
- `tests/test_ping.py`. `--kill` stops the local `daos_server` and re-probes,
  which is the half that matters — a test proving only that ping returns 0 when
  things work would not have caught the shortcut above.

### Fixed

- `connector.py` had no `logger`; the other modules in the package take one from
  `lmcache.logging`. It now does too, with a stdlib fallback so the module stays
  importable without LMCache, which is what the unit tests rely on.

### Added

- `VRAM_SEG` in the NIXL DAOS backend: transfers straight between DAOS objects
  and GPU memory via `daos_obj_fetch_gpu()` / `daos_obj_update_gpu()`. It is
  compiled in only when the client exports those symbols (meson checks;
  `DAOS_PREFIX` selects the client), so a stock DAOS never advertises a
  capability it cannot serve.

  It is **off by default and should stay off.** `doc/NIXL-DAOS-VRAM.md` has the
  measurement: device-to-device round trips are bit-identical, but the path is
  3.4x *slower* than staging through host memory. The cost is not bandwidth and
  not RPC count -- holding the bytes fixed and cutting the sgl entries from
  4800 to 1200 cut the time by 4.03x, which puts about 0.43 ms on every entry.
  That is CaRT re-registering the GPU buffer per transfer. The field meant to
  avoid it, `daos_mem_attr_t::ma_rkey`, carries a descriptor that only cuFile's
  plugin callback receives, so a backend calling DAOS directly cannot supply
  it; and Mercury pins `FI_MR_CACHE_MAX_COUNT=0`, so caching the registration
  is closed too.
- `nixl/tests/test_gpu.cpp` — the device-to-device round trip. The payload is
  produced on the device and compared from the device, so a silent fallback to
  a host bounce cannot pass it.
- `-g` in `nixl/tests/bench_nixl.cpp` to stage through GPU memory.

### Fixed

- The NIXL DAOS backend implements `loadLocalMD()`. It declared
  `supportsLocal()` true without it, and the base class answers that with an
  error rather than a default, so **every** `registerMem()` made through a
  `nixlAgent` failed and took transfers down with it. Nothing calls that method
  when the backend is driven directly, which is how it survived two test
  programs, a benchmark and a 34 GB/s measurement.

### Added

- `nixl/tests/test_agent.cpp` — drives the backend through a real `nixlAgent`
  rather than directly. It found the bug above, and it confirms the descriptor
  contract the other tests could only assume: `createXferReq()` passes
  `nixlBasicDesc` with no metadata pointer, so the agent matches a transfer
  descriptor back to its registered object by `devId` alone.

- `doc/NIXL-DAOS-MEASUREMENT.md` — the NIXL DAOS backend measured on the 400G
  verbs testbed: **34.17 GB/s** reading 4.69 GiB with 40 layers folded into one
  RPC, against 14.40 GB/s unfolded. Separating the two variables showed that
  effective concurrency is `min(threads, requests in flight)` and flattens at
  64, which is now the default pool size (`NIXL_DAOS_THREADS`).

  The document also retracts two readings made while measuring, both of which
  compared runs at mismatched concurrency. Thread *creation* was never the
  bottleneck — replacing thread-per-request with a pool moved the unfolded arm
  by 1.3% — and the NIXL layer is not free: matched against the raw object API
  at the same shape and thread count it costs roughly 0.076 ms per request,
  which folding hides (5% of a folded transfer, 60% of an unfolded one).
- `nixl/tests/bench_nixl.cpp` — the harness, shaped like `tests/obj_latency.c`
  so the two are comparable.

### Changed

- The NIXL DAOS backend runs transfers on a fixed thread pool instead of one
  thread per posted request. Completion moved from a `std::future` to an atomic
  counter, since one request can span several RPCs, and the first error wins so
  a later group cannot mask an earlier failure.
- The plugin's `meson.build` resolves DAOS itself rather than trusting the
  parent's `find_library('daos')`, which reports existence without a path and
  produced a configure that succeeded and a link that failed. It now searches
  for a prefix carrying `daos.h` and takes both header and library from it, so
  a packaged DAOS under `/usr` and a source build under `/var/daos-stockfull`
  both build with no arguments.

- `doc/LAYERWISE-MEASUREMENT.md` — measured LMCache's `use_layerwise` mode
  against the DAOS connector. It attaches with no code change and hits 100%,
  but at the same `chunk_size` the hit TTFT is 11.5x worse, because the object
  count multiplies by the layer count while the bytes stay the same. Fitting
  three object sizes separates a fixed cost of ~0.63 ms per object from a
  marginal 0.067 ms/MiB (15.7 GB/s): at 1 MiB objects, 90% of the time is
  overhead. Raising the IO pool from 16 to 128 threads made it 9-10% *worse*,
  so the cost is serialised, not concurrency-bound. Layerwise does win one
  thing: cold-miss TTFT is 2.03x better, since the store overlaps compute.
- `deploy/launchers/run_vllm_layerwise_ab.sh` — the A/B launcher used for it.
- `DAOS_WORKERS` environment override for the connector's IO thread pool.
  Default stays 16, so behaviour is unchanged.
- `tests/obj_latency.c` — the follow-up that reframes the result above. The
  layerwise penalty is a DFS cost, not a DAOS one. Rebuilt on the raw object
  API with dkey = chunk and akey = layer, the same 4800 x 1 MiB reads take
  245 ms instead of 3343 ms, and the fixed cost per object falls from
  ~0.63 ms to ~0.0137 ms. That clears the 0.056 ms the earlier note said
  layerwise would need to beat the best non-layerwise path. Folding all 40
  layers into one RPC as an iod array reaches 204 ms, a shape DFS cannot
  express. Unexpectedly, the arm that mimics today's layout (one akey
  holding all 40 MiB) is the slowest of the three at 571 ms.
  The DFS control arm does not yet complete; see the doc for what is
  therefore still unconfirmed.
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
