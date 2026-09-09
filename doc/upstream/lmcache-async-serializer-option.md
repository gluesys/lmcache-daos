# LMCache: make the async-loading prefetch serializer selectable

> **보류 (2026-09-09).** 제안 자체는 유효하다. `dev` 를 다시 확인했고 `AsyncSingleSerializer` 하드코딩과
> `AsyncMultiSerializer` 미사용 상태가 그대로였다. 폐기된 경로도 아니다 — `storage_manager.py` 에
> 파일 차원의 deprecation 표시는 없고 `StorageManager.put` 하나만 `batched_put` 으로 대체됐다고 적혀
> 있다. `storage_backend/connector/` 에는 RemoteConnector 계열 구현이 21 개 있어 여전히 활발하다.
>
> 낮춘 이유는 근거의 폭이다. 이 직렬화기를 지나는 우리 백엔드는 둘인데, in-process `DaosConnector`
> 에서는 async loading 이 이득이 없었고(집계 평평, 단건 172→288 ms 악화 — `lmcache_daos/connector.py`
> 의 `support_batched_get_non_blocking` 주석 참조) 이득이 나온 곳은 **experimental** 로 표시한 GDS
> 백엔드뿐이다(Part B 집계 21.7 → 27.7 GB/s). 실험적 백엔드 한 곳의 수치로 상류 API 변경을 요구하는
> 모양새가 된다.
>
> 다시 집는다면 순서는 이렇다. 먼저 이슈로 띄워 `AsyncMultiSerializer` 가 왜 사장돼 있는지 상류 의중을
> 묻고, 근거를 "prefetch 지연이 병목인 in-process 백엔드" 로 일반화한 뒤 PR 을 낸다.

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
