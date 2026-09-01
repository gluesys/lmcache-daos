# LMCache MP 모드 L2 어댑터 평가 — DAOS 백엔드 관점

작성 2026-09-01. 근거는 **LMCache v0.5.2 sdist**(PyPI, 이 저장소가 핀한 버전) 소스를 직접
읽은 것이고, 인용은 모두 `파일:줄` 로 남겼다. 그림은
[`doc/figures/fig5_mp_l2_vs_connector.png`](figures/fig5_mp_l2_vs_connector.png).

이 문서가 있는 이유: "우리가 구현한 `RemoteConnector` 가 LMCache의 유일한 확장점인가",
"MP 모드로 옮기면 우리가 없앤 복사가 돌아오는가" 라는 두 질문에 답하려면 상류 소스를
읽어야 하고, 그 조사를 매번 반복하지 않기 위해서다. **Phase 5 를 시작하는 사람이 먼저
읽을 문서다.**

---

## 1. 한 문장

LMCache 에는 우리가 쓴 `RemoteConnector` 말고 **MP 모드 전용 `L2AdapterInterface`** 라는
두 번째 확장점이 있고, 그쪽이 우리 미해결 항목 다섯 개를 인터페이스 차원에서 이미 풀어놨다.
**옮겨도 복사는 늘지 않는다** — 단 SHM 전송 컨텍스트가 켜져 있어야 하고, 아니면 pickle
폴백으로 떨어져 **복사가 2회 늘어난다.**

## 2. 두 확장점

| | `RemoteConnector` (우리) | `L2AdapterInterface` |
|---|---|---|
| 호출 위치 | vLLM 워커 프로세스 안 | 별도 LMCache 서버 (컨트롤러 스레드 2개) |
| API 형태 | `async def get / put / exists / list` | `submit_*` → `query_*` / `pop_*` (논블로킹) |
| 완료 통지 | `await` | **eventfd 3개** (store / lookup / load) |
| 버퍼 소유 | 커넥터가 `allocate()` 해서 반환 | **호출자가 준다.** 어댑터는 수명 관리 금지 |
| 오류 단위 | 청크당 `None` | store=태스크, lookup·load=**키별 Bitmap** |
| 잠금 | 없음 | `lookup_and_lock` / `submit_unlock` |
| 용량·축출 | `list()` + `remove_sync()`, 정책 없음 | `get_usage` / `list_l2_keys` / `L2EvictionPolicy` |
| 다중 백엔드 | 하나 | `--l2-adapter` 반복 = 캐스케이드 |
| 키 | `CacheEngineKey` → sha256 (복원 불가) | `ObjectKey`(model / rank / group / hash / salt) |
| out-of-tree | `plugin://` 스킴 | `plugin_l2_adapter` (모듈 동적 로드) |

추상 메서드는 `lmcache/v1/distributed/l2_adapters/base.py:152-330` — `get_*_event_fd`,
`submit_store_task`, `pop_completed_store_tasks`, `submit_lookup_and_lock_task`,
`query_lookup_and_lock_result`, `submit_unlock`, `submit_load_task`, `query_load_result`.
선택 메서드로 `register_listener` / `supports_global_eviction` / `list_l2_keys` /
`get_usage` (같은 파일 353-499).

**파이썬 `RemoteConnector` → L2 브리지는 없다.** `native_connector_l2_adapter.py` 가
브리지처럼 보이지만 대상이 **pybind 로 감싼 C++ `IStorageConnector`** 이고(파일 헤더
주석), native 쪽 eventfd 1개 + `drain_completions()` 를 파이썬 eventfd 3개로 demux 한다.
즉 DAOS L2 어댑터는 새로 써야 한다 — 상류 어댑터들이 800~1,300줄대다.

## 3. MP 모드 데이터 경로

문서: `docs/source/mp/l2_storage/index.rst`.

- **L1**(빠른 계층) — 기본 CPU 메모리, `--gds-l1-path` 지정 시 cuFile NVMe 슬랩.
- **L2**(영속) — 어댑터들. `StoreController` 가 eventfd 로 신규 객체를 감지해 L2 로 밀고,
  `PrefetchController` 가 미스 시 L2 → L1 으로 끌어올린다.
- vLLM 은 `STORE` / `LOOKUP` / `RETRIEVE` RPC 로만 붙는다.

## 4. 복사 계수 — 늘지 않는다

| | 현재 (in-process 커넥터) | MP 모드 + SHM 컨텍스트 |
|---|---|---|
| ① | DAOS → `MemoryObj.byte_array` (`dfs_sys_read` 가 목적지에 직접 write) | DAOS → `obj.byte_array` = **L1 shm 슬롯** |
| ② | `MemoryObj` → GPU (`c_ops` H2D) | 워커가 shm 뷰에서 H2D |
| **합계** | **2회** | **2회 — 동일** |

프로세스 경계에서 추가 복사가 없는 근거는 `v1/multiprocess/transfer_context/shm.py` 다.

- 워커가 서버가 만든 L1 풀 shm(`lmcache_l1_pool_*`)을 **그대로 매핑**하고 resource tracker
  에서 unregister 해 소유권을 서버에 남긴다 (`shm.py:105-118`).
- 그 shm 을 워커 프로세스에서 **`cudaHostRegister` 로 핀**한다 — 주석이 *"pin memory here
  is for worker side for fast DMA copy"* (`shm.py:238`).
- retrieve 시 IPC 로 오가는 것은 데이터가 아니라 **슬롯 디스크립터
  `(offset, length, shape, dtype)`** 뿐이고, 워커가 그걸로 shm 위에 텐서 뷰를 만든다
  (`ShmSlotDescriptor`, `_make_tensor_view` — `torch.frombuffer(self._shm_buffer, ...,
  offset=offset)`).

그리고 어댑터가 호출자 버퍼에 직접 쓰는 것이 **인터페이스의 요구사항**이다 —
`submit_load_task` docstring: *"The L2 adapter will write the loaded data to the memory
buffer provided by the caller."* (`base.py:304-327`). 상류 byte-array 어댑터들도 그대로
`obj.byte_array` 를 쓴다 (`fs_l2_adapter.py:591`, `bigtable_l2_adapter.py:1108`).
**우리가 `_get_sync` 에서 확보한 무복사 read 기법이 그대로 이식된다.**

### 함정 — pickle 폴백이면 복사 +2

`_compute_shm_pool_info()` 가 **빈 풀**을 돌려주는 조건 (`multiprocess/engine_context.py:293-299`):

- `shm_name` 이 비었을 때
- `use_lazy` 가 켜졌을 때
- `devdax_path` 가 설정됐을 때

이때 전송이 `EngineDrivenContextPickle` 로 떨어진다
(`multiprocess/transfer_context/pickle.py`). store 는 청크를 pickle 직렬화해
`COMMIT_STORE` 로 보내고, retrieve 는 받은 바이트를 역직렬화한다 → **직렬화 버퍼 +
역직렬화 텐서로 복사 2회 추가.** MP 모드에서 복사가 실제로 늘어나는 유일한 경로이고,
설정 실수로 조용히 빠질 수 있으므로 **측정 전 체크 항목**이다.

## 5. 성립 조건 3개

1. **SHM 전송 컨텍스트 활성** — 위 함정을 피할 것.
2. **byte-array 어댑터**로 호출자 버퍼에 직접 write.
3. **GDS L1(`--gds-l1-path`) 과는 병용 불가** — 문서가 *"Byte-array L2 adapters are
   unsupported under the GDS L1 tier, which exposes no L1 memory buffer"* 로 명시
   (`docs/source/mp/l2_storage/index.rst`). 다만 [`gpudirect/README.md`](../gpudirect/README.md)
   에서 **DAOS 에 cuFile 드라이버가 없다**는 결론이 이미 났으므로 실질 제약은 아니다.

## 6. 우리 미해결 항목이 어떻게 되는가

| 우리 항목 | L2 인터페이스에서 |
|---|---|
| 스트리밍 / `read ⊕ H2D` 오버랩 — `stream_get` 을 만들고도 **호출자가 없었다** | `submit` + eventfd + 태스크별 조회로 표현 가능 |
| 용량 정책 부재 | `get_usage` · `list_l2_keys` · `L2EvictionPolicy` 훅 |
| `list()` 이름의 키 복원 불가 | `ObjectKey` 가 구조적이라 문제 자체가 없음 |
| 청크별 오류 보고 | `Bitmap` 으로 키 단위 성공/실패 |
| `_drop_put_ref` 참조 카운트 다툼 | 호출자가 버퍼를 주므로 소유권 문제 소멸 |
| 무복사 read | `obj.byte_array` 직접 write — 그대로 유지 |

**부수 효과**: L1 이 노드 단위 공유 풀이므로 같은 노드의 다른 LMCache 클라이언트나 재시작한
프로세스가 같은 청크를 **L2 에서 다시 읽지 않는다.** 복사 감소가 아니라 요청 감소이며,
"2노드 집계가 단일 클라이언트 raw 천장에 막힌다"던 구간에 직접 작용한다.

**옮겨도 그대로인 것**:

- **DAOS 읽기 데이터 손상** — 인터페이스와 무관한 DAOS 내부 결함. 동시성이 필수 조건도 아니다(단일 writer/reader 로도 재현)
  ([`gpudirect/DAOS-CONCURRENT-READ-CORRUPTION.md`](../gpudirect/DAOS-CONCURRENT-READ-CORRUPTION.md) §12).
- **진짜 batch RPC 부재** — 여전히 청크당 객체 1개. 접으려면 dkey/akey 레이아웃이 필요하다.
- **DFS chunk 규칙** — `chunk ≈ 파일크기 ÷ 랭크당 타깃수` 는 그대로 적용된다.
- **파이썬 스레드풀** — 어댑터도 blocking `libdfs` 를 스레드로 감싼다 (부록 참고).

## 7. 비용과 미측정 항목

- **RPC 왕복** (`PREPARE_RETRIEVE` → `COMMIT_RETRIEVE`, store 쪽 대응 쌍) 이 TTFT 에
  더해진다. `prepare_retrieve` 가 슬롯 리스트를 한 번에 돌려주므로 청크당이 아니라
  **요청당 상수 항**으로 보이지만 **미측정**이다.
- 별도 프로세스 운영, 어댑터 재작성(800~1,300줄), pickle 폴백을 피하는 설정 규율.

## 8. 참고 사례 — `SageMakerHyperPodL2Adapter`

L2 어댑터가 실제로 어떤 모양인지 보려면 이게 대표적이다
(`l2_adapters/sagemaker_hyperpod_l2_adapter.py` 825줄 + `sagemaker_hyperpod_client.py` 515줄).

AWS SageMaker HyperPod 의 **node-local `ai-toolkit` 데몬**을 저장소로 쓰고, 읽기와 쓰기가
비대칭이다.

- **읽기**: `acquire_lease(key)` HTTP → 데몬이 **POSIX 공유메모리의 (offset, length) 리스**를
  발급 → `copy_from_lease(lease, destination)` 로 호출자 버퍼에 memcpy → `release_lease`.
  **HTTP 는 제어 평면이고 데이터 평면은 shm memcpy 다.**
- **쓰기**: HTTP chunked PUT (`put_stream_chunk_bytes` 기본 64 KiB).

데몬이 `hostNetwork` 9200 의 노드 로컬이라 LMCache 서버가 같은 노드에 있어야 한다
(`docs/source/mp/l2_storage/sagemaker_hyperpod.rst`). 어댑터 코드에서 보이는 범위는 노드
로컬 shm 까지이고, 그 뒤 클러스터 계층화 여부는 어댑터 밖의 일이라 확인되지 않는다.

## 9. 권고

**지금 옮기지 않는다.** DAOS 읽기 손상이 해소되기 전에는 어느 인터페이스로 붙여도
E2E 결과가 같으므로 착수 이유가 없다.

**Phase 5 방향으로는 `RemoteConnector` 개선보다 L2 어댑터가 낫다.** 우리가 상류에 RFC 로
요청하려던 스트리밍 API 가 거기엔 이미 있고, 미정으로 남긴 용량 정책도 훅이 준비돼 있다.

착수 조건과 순서:

1. DAOS 손상 해소 (선행 필수)
2. `read ⊕ H2D` 오버랩을 L2 인터페이스에서 프로토타입 — 여기서 1.55~1.73x 가 재현되는지
3. RPC 왕복 상수 항 측정 — TTFT 예산에서 차지하는 비율
4. 그다음에 어댑터 본체 (`plugin_l2_adapter` 경유 out-of-tree)

## 부록 — 상류 커넥터 18개의 동시성 방식

"파이썬 스레드풀이 적절한가" 에 대한 조사 기록. v0.5.2 커넥터 전수.

| 방식 | 커넥터 |
|---|---|
| **파이썬 스레드 오프로드** (우리와 동일) | `valkey`(TPE, num_workers=8, per-thread client), `hf3fs`(TPE), `mooncakestore`(`to_thread`), `hfbucket`(`to_thread`), `infinistore`(`run_in_executor`), `fs`(aiofiles = 내부가 스레드풀) |
| 네이티브 async 클라이언트 | `redis`(`redis.asyncio`), `azure`(aio SDK), `sagemaker_hyperpod`(aiohttp), `lm`(asyncio 소켓), `bigtable`(코루틴 + CPU 작업만 executor) |
| C 런타임이 자체 스레드 | `s3`(awscrt + `on_done` 콜백), `eic`(네이티브 SDK + ctypes) |

**스레드를 안 쓰는 커넥터는 예외 없이 클라이언트가 async 이거나 C 런타임이 자기 스레드를
갖고 있는 경우다.** `libdfs` 는 둘 다 아니다 — blocking C API 이고, DAOS 의 async(event
queue)는 EQ 당 `eqx_lock` 직렬화로 7–12 GB/s 에 묶여 블로킹 풀(34.3 GB/s)보다 느리다.

상류가 오히려 반대 방향으로 옮긴 사례가 근거다 — `valkey_connector.py:1-9`:

> "ValkeyConnector — **high-throughput** Valkey connector using the GLIDE **sync** client …
> with a ThreadPoolExecutor and per-thread clients. **This replaces the legacy async
> ValkeyConnector** … that used the async GLIDE client"

LMCache 본체의 로컬 디스크 백엔드도 `AsyncPQThreadPoolExecutor`(내부가 `asyncio.to_thread`)
를 쓴다 (`local_disk_backend.py:52`).

차용할 것 하나: LMCache 는 `job_executor/pq_executor.py` 에 **우선순위 큐 실행기**를 두고
`PEEK > PREFETCH > GET > PUT` 로 스케줄한다(valkey·s3·eic·redis 공용). 우리는 평평한
`ThreadPoolExecutor` 라 대량 store 가 지연 민감한 lookup 앞을 막을 수 있다 — 동시성이
올라갈 때 볼 수 있는 실제 차이다.

## 근거 파일 목록

```
lmcache/v1/distributed/l2_adapters/base.py                  L2AdapterInterface (552줄)
lmcache/v1/distributed/l2_adapters/plugin_l2_adapter.py     out-of-tree 동적 로드
lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py  C++ pybind 전용 브리지
lmcache/v1/distributed/l2_adapters/sagemaker_hyperpod_l2_adapter.py
lmcache/v1/distributed/memory_manager/l1_memory_manager.py  L1 = POSIX shm 풀
lmcache/v1/multiprocess/transfer_context/shm.py             shm 매핑·핀·슬롯 뷰
lmcache/v1/multiprocess/transfer_context/pickle.py          폴백 (복사 +2)
lmcache/v1/multiprocess/engine_context.py                   _compute_shm_pool_info
lmcache/v1/storage_backend/job_executor/pq_executor.py      우선순위 큐 실행기
lmcache/v1/storage_backend/connector/*.py                   커넥터 18개
docs/source/mp/l2_storage/                                  MP L2 문서
```

재현: `pip download --no-deps --no-binary :all: lmcache==0.5.2` 또는 PyPI JSON API 로
sdist 를 받아 펼친다. 실행은 필요 없다 — 읽기만 했다.
