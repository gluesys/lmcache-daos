# LMCache: make the async-loading prefetch serializer selectable

**Where**: `lmcache/v1/storage_backend/storage_manager.py`, `StorageManager.__init__` (0.5.2 and `dev` as of 2026-09-06):

```python
        if not self.enable_pd and self.config.enable_async_loading:
            assert self.allocator_backend is not None
            self.async_serializer = AsyncSingleSerializer(self.loop)
```

`AsyncSingleSerializer` wraps every `batched_get_non_blocking()` in one `asyncio.Lock`, so prefetches for different lookups
never overlap. `AsyncMultiSerializer` (same file) already implements a chunk-budget weighted semaphore over
`allocator_backend.calculate_chunk_budget()`, but nothing instantiates it.

**Why it matters**: with a storage backend whose reads are latency-bound rather than CPU-bound (GPU-direct DAOS, 23–25 ms per
640 MiB request), single-file prefetch caps the aggregate at one request's bandwidth. Measured on one H100 client, 100 GB
working set, 12 concurrent requests, 4096-token documents:

| serializer | avg TTFT | p95 | aggregate |
|---|---|---|---|
| single (current) | 303–311 ms | 349–356 ms | 25.5–26.1 GB/s |
| multi (chunk budget = GPU pool / chunk) | 285–302 ms | 320–345 ms | 26.5–27.7 GB/s |

**Proposal** (illustrative diff; extra_config keeps the default behaviour):

```diff
         if not self.enable_pd and self.config.enable_async_loading:
             assert self.allocator_backend is not None
-            self.async_serializer = AsyncSingleSerializer(self.loop)
+            kind = (self.config.extra_config or {}).get("async_loading_serializer", "single")
+            if kind == "multi":
+                self.async_serializer = AsyncMultiSerializer(self.allocator_backend, self.loop)
+            else:
+                self.async_serializer = AsyncSingleSerializer(self.loop)
```

**Caveat worth discussing upstream**: `AsyncMultiSerializer` sizes its budget from the *storage manager's* allocator backend
(LocalCPUBackend). A plugin backend that allocates in its own pool (an `AllocatorBackendInterface` returning itself from
`get_allocator_backend()`) needs the budget of *its* pool, otherwise the semaphore protects the wrong resource. Two options:
pass the backend that will serve the prefetch into `run()`, or let plugins supply the serializer. Our out-of-tree workaround
rebinds `storage_manager.AsyncSingleSerializer` to a factory building `AsyncMultiSerializer` over the GPU pool at plugin import
(`lmcache_daos/gds_backend.py::_install_multi_prefetch_serializer`), which is the kind of hack an option would remove.

Also observed while writing the plugin (documentation-worthy in the plugin guide):
- `batched_contains()` must return the **prefix hit count (int)**; the manager slices `keys[:n]`. Returning a list breaks the
  lookup server with `slice indices must be integers` and every lookup then times out (3 s) → TTFT ×5.
- `RemoteMetadata.serialize()` asserts a process-global `REMOTE_METADATA_FMT` that only `RemoteBackend` initialises, so a
  storage plugin cannot use it for its own on-disk metadata.
