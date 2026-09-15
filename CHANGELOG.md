<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the
project intends [Semantic Versioning](https://semver.org/) from its first tag.

No interface here carries a compatibility promise yet; the leading `0.`
is doing real work.

## [Unreleased]

### Removed

- `.github/workflows/ci.yml`. The GitHub repository is a push mirror whose token
  lacks the `workflow` scope, so GitHub rejected every push touching that file
  and the mirror had been stuck at 2026-09-09. CI is unchanged and runs from
  `.gitlab-ci.yml`; `.github/README.md` records the reason.

### Added

- `tests/bench_layerwise_ab.py` measures DFS against dkey/akey through the real
  connector, and phase 1 step 5's stopping condition has fired: the benefit is
  not there.

  Per object, best of three, 40 objects: the raw path wins 1.21x on a 64 KiB
  read and loses from there -- 0.66x at 1 MiB, 0.46x at 4 MiB, 0.36x at 16 MiB.
  None of the 46x survives.

  The direction is the diagnosis. A fixed-cost advantage should be largest on
  small objects and converge to 1x on large ones; instead it drops below 1x,
  which points at bandwidth rather than per-object cost. The likely reason is
  placement: a dkey determines the shard, so one chunk under one dkey puts a
  whole object on a single target, while DFS spreads a file across dkeys by
  offset and stripes over all of them. That explanation comes from the shape of
  the table and DAOS's placement rule, not from a measurement of per-target I/O.

  Not a dead end, but the plan says stop, so step 6 (extending to MP and GDS) is
  not being started. The fix to try is striping the payload over several dkeys,
  or using the DAOS array API the DFS path already sits on.

### Fixed

- `_drop_put_ref` released a reference that was never ours, freeing objects
  LMCache still read. `CreateConnector` wraps every connector in
  `InstrumentedRemoteConnector`, which drops it in a `finally` for both `put`
  and `batched_put`; `AzureConnector.put` says so in words ("this method must
  not call `ref_count_down` itself"). Ours was a second drop, measured taking
  the count `1 -> 0` and invalidating the object, which is the assertion that
  killed the vLLM engine on every cache hit.

  With it gone the correctness gate runs to completion, and the result is
  identical in three configurations -- no DAOS, DAOS over DFS, DAOS over
  dkey/akey: 4 match, 2 mismatch, 0 inconclusive, the same prompts and the same
  generated text down to the character. The remaining mismatch is therefore not
  storage; the backend reproduces the reference configuration exactly.

  No upstream issue was filed. Two earlier claims here were wrong and are
  retracted: the connector docstring's reading of who owns the serializer's
  reference, and a file-level grep that reported six built-in connectors
  dropping it -- those calls are get-path error cleanup and have nothing to do
  with put.

### Added

- Two diagnostics behind environment flags, both off by default:
  `DAOS_DEBUG_OBJ` logs the reference count either side of `_drop_put_ref`, and
  `DAOS_SKIP_PUT_REF_DROP` skips the drop (which leaks, deliberately).

  `DAOS_SKIP_PUT_REF_DROP` has since been removed: skipping the drop is no
  longer a diagnostic, it is the behaviour. `DAOS_DEBUG_OBJ` stays.

  Together they closed the question the correctness gate was blocked on. Our
  drop was the one that freed the object: `1 -> 0 (pin=0 valid=False)`, after
  which `.tensor` returns None and the assertion fires. Skipping it makes the
  crash disappear entirely -- pass B goes 500 to 200, invalidations 1 to 0.

  It is not obviously our bug. LMCache's own connectors are split on whether
  they release that reference (bigtable, hf3fs, azure, hfbucket, sagemaker and
  instrumented do; redis and fs do not), and `cache_engine.py:562` says so
  itself: "TODO: we implicitly rely on batched_put to call ref_count_down /
  this management should be done in a cleaner way". Ours matches the majority,
  and here the majority behaviour frees an object LMCache still reads.

  Every option costs something -- drop and crash, skip and leak a reference per
  stored chunk, or fix it upstream -- so the next move is to strengthen LMCache
  issue #5090 with this minimal reproduction and that TODO.

### Fixed

- A docstring in `connector.py` quoted `NaiveSerializer.serialize` as ending in
  `return self._dbg(memory_obj, "read")`. It does not; the previous commit's
  blind replacement of `return memory_obj` reached into quoted upstream source.
  Restored, and checked against the real file.

### Added

- `tests/raw_gate_ab.sh` runs the correctness gate against both layouts, control
  first, and `tests/kv_correctness_gate.sh` gains `LOG_CMD`, `GATE_MODEL` and
  `PARA_REPEAT` -- it was fixed to one podman deployment, one model name and one
  prompt length, and each mismatch arrives looking like a backend failure.

  Phase 1 step 4 is NOT passed. The gate dies at
  `gpu_connectors.py:285 assert memory_obj.tensor is not None` during retrieve,
  taking the vLLM engine with it -- **on the DFS control as well as on the
  object API**, identically. Nothing about the raw path can be claimed from a
  run whose known-good arm fails the same way. Recorded in
  [doc/RAW-API-PLAN.md].

  Root cause found. The object whose `.tensor` is None is one that has already
  been **invalidated** -- LMCache warns "Trying to access an invalidated
  MemoryObj" milliseconds before the assertion. It is a use-after-free, it
  happens only when a remote backend is configured (local_cpu alone: zero
  invalidations, zero asserts, both passes 200), and it does not distinguish
  DFS from dkey/akey. Logging every successful return of `_get_sync`/`_get_raw`
  produced nothing in a failing run, so the object is not one we hand back --
  which leaves the store path, where `_prep_write` aliases a MemoryObj and
  `_drop_put_ref` puts a reference down. Same family as the double-unpin this
  project filed as LMCache issue #5090, though not demonstrably the same bug:
  #5090 reproduces without DAOS and this needs a remote backend.

  What the attempt also established: backend selection works under real vLLM, with
  each arm logging which one it chose; a non-POSIX container serves a vLLM
  startup and LMCache init without complaint; and the gate reported
  INCONCLUSIVE rather than a false pass when vLLM's own prefix cache answered
  pass B and LMCache was never consulted.

- Phase 1 step 3 of [doc/RAW-API-PLAN.md]: `tests/test_torn_object_raw.py` holds
  the torn-object gate for the dkey/akey layout. Nine damage shapes, all reading
  back as a miss, with the staging buffer handed back every time.

  The shapes are different even though the policy is not. In a file, torn means
  short; under dkey/akey it means an absent akey, which DAOS reports with rc 0
  and an untouched `iov_len` -- so "absent" and "a full buffer of uninitialised
  memory" arrive looking the same unless the code reads `sg_nr_out`. The case
  the write order exists for is covered directly: payload written, metadata not.

  It drives the real connector instead of a copy of its read path. The DFS
  version re-implements `_get_sync` because importing the connector needs
  LMCache, and that duplicate has to be kept in lockstep by hand; a read path
  that has drifted from the one in production proves nothing. Damage is injected
  at the storage layer -- punch and overwrite akeys under a real object -- rather
  than by building headers, because that is what a crashed writer leaves.

- Phase 1 step 2 of [doc/RAW-API-PLAN.md]: the connector picks its backend from
  the container. `ObjSys` does the dkey/akey I/O, `container_layout()` reads
  DAOS_PROP_CO_LAYOUT_TYPE, and a POSIX container still gets the DFS path
  byte-for-byte. Verified both ways on one connector against one pool.
  `DAOS_FORCE_LAYOUT` overrides the probe and is rejected when it disagrees
  with the container, because a silent disagreement is the failure this exists
  to prevent.

  Two DAOS behaviours were measured rather than assumed, and both would have
  shipped as silent bugs:

  A fetch of an akey that is not there returns **rc 0** and does **not** clear
  `iov_len` -- it keeps whatever the caller put in, i.e. the request size. A
  reader trusting it serves a full buffer of uninitialised memory as a hit.
  `sg_nr_out` is the real gate; the table is in `ObjSys.fetch`.

  A `DAOS_IOD_SINGLE` value is fetched whole or not at all: a buffer smaller
  than the stored value returns `DER_REC2BIG(-2013)` rather than a short read.
  Arrays do not behave this way. `exists()` probed with only the fixed header
  size and every call failed.

- Phase 1 step 1 of [doc/RAW-API-PLAN.md]: `lmcache_daos/obj_binding.py` and
  `lmcache_daos/serde_v3.py`, with tests that need no DAOS. Nothing is wired
  into the connector yet and no default moves.

  `serde_v3` is a placement, not a new header format. The metadata akey holds a
  v2 header with the page padding removed, and `parse_meta` delegates to
  `serde_v2.parse_header` unchanged, so the torn-object policy transfers
  instead of being re-proved. Two things fall out of the placement: no padding
  (nothing follows the header to align) and no rename (the metadata akey is the
  commit record -- payload akeys first, metadata last, and a crash between
  leaves a dkey a reader reports as a miss).

  Writing both in one `daos_obj_update` is tempting and deliberately not done:
  a dkey's akeys can land on different shards under replication or EC, so one
  call is not one commit point. The extra RPC is against a few dozen bytes.

  `obj_binding` pins the ABI. `daos_iod_t` carries `iod_flags` between
  `iod_size` and `iod_nr`; omitting it yields a binding that compiles, links
  and corrupts, so the test asserts size 64 and every offset as literals
  cross-checked against the C compiler on a host with the headers. The builders
  return everything that must outlive a transfer, because ctypes frees an
  unreferenced temporary immediately and DAOS would read the dangling pointer.

- [doc/RAW-API-PLAN.md] scopes phase 1 of the move to dkey/akey: the in-process
  connector only, behind container-type detection, changing no default and no
  existing data.

  Two findings shape it. The key mapping can fold LAYERS but not CHUNKS --
  `CacheEngineKey` carries no prefix identifier, so a reader holding only keys
  cannot reconstruct which chunks belonged together, while the layers of one
  chunk share a derivable dkey. And mode cannot be a runtime toggle: container
  layout is fixed at creation, a POSIX container rejects `daos_obj_update` and
  a non-POSIX one rejects `dfs_sys_connect`, so the container decides and the
  connector should detect rather than be told.

  Step 5 is a gate, not a formality: 46x is a microbenchmark difference in
  fixed cost, and how much survives the engine overhead on the real path is
  still unmeasured. If it does not survive, the plan stops there.

### Changed

- `NIXL_DAOS_EQ_TIMEOUT` is now **on by default at 60 s**. The measurement it
  was waiting for exists: on client-5 against cell1/cell2 over 400G verbs --
  the regime the event queue was originally rejected in -- four runs each gave
  31.04 GB/s blocking against 30.64 event queue, with overlapping ranges and
  one run where the event queue was faster. Unfolded, 7.55 against 7.50. The
  old "caps around 7-12 GB/s" does not transfer to folded object-API requests.

  A deadline that costs nothing and turns 16 permanently lost threads into a
  bounded error is not a trade. 60 s is a backstop rather than a latency
  target: a request measured 1.34 ms there. `NIXL_DAOS_EQ_TIMEOUT=0` restores
  the blocking call.

### Added

- `NIXL_DAOS_EQ_TIMEOUT` gives the NIXL backend a deadline it owns. Set to a
  number of seconds, the backend submits to a DAOS event queue and polls with
  that timeout instead of making a blocking call. Killing `daos_server`
  mid-read, through the plugin: blocking was still running at 120 s with 65
  threads stuck; with the timeout at 8 s the process exited in 8 s and every
  thread came back.

  Off by default, and the reason is worth keeping. The old rejection of the
  event queue ("caps at 7-12 GB/s however the queues are arranged") came from a
  sweep over the DFS async path through Python with 28 MiB reads, and it does
  not reproduce for folded object-API requests. But it is not refuted either:
  cxl2 is single-node TCP and tops out near 3 GB/s, where the fabric is the
  bottleneck and both paths look alike. The 400G verbs regime the rejection
  came from has not been re-measured.

  The first implementation held one queue per worker thread and was 35% SLOWER
  than blocking (2.08 vs 3.12 GB/s): the pool is deliberately oversized, and
  while an idle thread is free, an idle event queue holds a network context --
  64 contexts to run 16 concurrent requests. Borrowing a queue per request
  instead grows the set to the actual concurrency and no further, and lands at
  3.24 GB/s independent of thread count.

- Resolved from the above: the GPU entry points take an event after all. The
  plugin comment claiming "synchronous only -- no event queue", and using that
  to justify the thread pool, was wrong. In `theodore/b_cufile`,
  `src/client/api/object.c:213` differs from `:197` only by the GPU_DIRECT flag
  and `args->mem_attrs`; `ev` is passed to the same task machinery. So the
  `nullptr` in the plugin is a choice, and the VRAM_SEG path can have the same
  bounded escape. Verified against the draft branch's source, not against the
  binary on the GPU host, and the GPU path's failure behaviour is still
  unmeasured.

- `tests/obj_failure.c` measures the low-level object API under the same fault,
  because switching to dkey/akey for its 46x lower per-object cost should not be
  decided without knowing what it does when the engine dies.

  The answer is that the interesting axis is not the one people assume.
  `daos_obj_update(..., NULL)` wedges exactly like DFS -- 16 of 16 threads still
  inside the call at 150 s, indistinguishable. What DFS cannot do at all is the
  event queue: with `daos_eq_poll` on a timeout, all 16 threads gave up at
  5.01 s and none was lost, and `daos_event_abort` + `daos_event_fini` then cost
  0.00 s each, so the tidy version of that pattern is bounded too.

  That bound does not depend on the open question of whether the endless retry
  is a single-rank artifact, because the deadline belongs to the caller rather
  than to DAOS. The NIXL plugin currently passes `nullptr`, so it has the
  blocking behaviour today; moving to the raw API buys performance and, on its
  own, changes nothing about failure.

- A fault matrix for the DAOS backend -- `tests/failure_modes.py` (one scenario
  per process), its driver `tests/failure_modes.sh`, and the record in
  [doc/FAILURE-MODES.md]. Every number in this repo up to now came from a
  healthy pool, which says nothing about the case that decides shippability.

  Good news first: **no scenario produced wrong bytes.** A destroyed container
  raises `DER_NO_HDL` immediately, a full pool raises `ENOSPC` in 0.01 s and
  goes on serving reads, and killing `daos_agent` under an open handle changes
  nothing at all -- the agent is not on the I/O path once the handles exist.

  The bad one: **a DAOS call whose engine has died does not return.** SIGKILL of
  `daos_server` during a `batched_put` left 16 of 16 `daos-io` threads inside
  `dfs_sys_write`, still there 16 minutes later, spinning ~13 cores.
  `CRT_TIMEOUT=10` changed nothing. A thread inside a C call cannot be cancelled
  from Python, so the pool is finished for the life of the process.

### Fixed

- `close()` no longer joins threads that are never coming back. It called
  `self._pool.shutdown(wait=True)` and hung there -- the comment two lines below
  had already worked this out for the ping pool and chosen `wait=False`, while
  the data pool kept `wait=True`. Note this does not make the process
  *exitable*: `concurrent.futures` joins every worker at interpreter shutdown
  regardless, verified directly.

### Changed

- The connector notices when its IO pool is wedged and fails immediately
  instead of queuing behind it (`DaosPoolWedged`, rc `EBUSY`; threshold
  `DAOS_STALL_SECS`, default 60 s). Queuing forever is strictly worse than
  failing: LMCache treats a raised `get`/`put` as a miss and serves from the
  model, while a submission that never returns takes the request with it.

  The test is "every worker busy AND no completion in the window", not
  saturation -- a large store legitimately occupies all 16 workers for minutes,
  and a detector that could not tell load from a wedge would disable the
  backend under the load it exists to serve. `ping()` reports it too, since the
  probe runs on its own thread and would otherwise return a cheerful 0 from a
  dead data path. Cost is 0.59 us per operation, 0.78% of the measured 76 us
  per-request NIXL overhead.

  Same structure exists in the MP adapter, the NIXL plugin and the GDS path;
  only the in-process connector is guarded so far.

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

[doc/FAILURE-MODES.md]: doc/FAILURE-MODES.md
[doc/RAW-API-PLAN.md]: doc/RAW-API-PLAN.md
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
