# LMCache MP(multiprocess) 모드용 DAOS L2 어댑터 — 구현 계획

작성 2026-09-04. 브랜치 `mp-mode`. 근거 소스는 client-5 이미지 `kvsup-ucx-lmc:local` 의
LMCache 0.5.2 (`lmcache/v1/multiprocess`, `lmcache/v1/distributed`).

## 0. 한 줄 결론

MP 모드는 **별도 프로세스(ZMQ 서버)가 L1(pinned CPU)과 L2 를 소유**하고, L2 는 `RemoteConnector`
가 아니라 **`L2AdapterInterface`**(eventfd 기반 비동기 배치 계약)로 붙는다. 따라서 기존
`DaosConnector`(in-process `LMCacheConnectorV1` 전용)는 재사용이 안 되고, **`lmcache_daos.mp` 패키지에
DAOS L2 어댑터를 새로 구현**해야 한다. `dfs_binding.DfsSys` 와 경로·프레이밍 규칙은 재사용한다.

## 1. MP 모드가 실제로 어떻게 동작하는가 (소스로 확인한 것)

| 구성요소 | 사실 |
|---|---|
| 서버 | `python -m lmcache.v1.multiprocess.server` — ZMQ(`--host/--port`, 기본 5555). `--l1-size-gb`(pinned CPU L1, 필수), `--eviction-policy {LRU,IsolatedLRU,noop}`(필수), `--chunk-size`, `--max-gpu-workers/--max-cpu-workers`, `--hash-algorithm blake3`, `--supported-transfer-mode {lmcache_driven,engine_driven,auto}`, **`--l2-adapter '<JSON>'`**(반복 가능) |
| vLLM 측 | `kv_connector: "LMCacheMPConnector"`(vLLM factory 에 등록돼 있음). `kv_connector_extra_config` 로 `lmcache.mp.host`(기본 `tcp://localhost`), `lmcache.mp.port`(5555), `lmcache.mp.mq_timeout`, `lmcache.mp.heartbeat_interval` |
| 스토리지 | `lmcache.v1.distributed.storage_manager.StorageManager`. L1 매니저 + L2 어댑터 목록. 토큰 해시는 **서버가** 계산(blake3) → in-process 모드의 키와 **호환되지 않는다**(별 네임스페이스로 봐야 함) |
| L2 등록 | 모듈 import 시 `register_l2_adapter_type("daos", ConfigCls)` + `register_l2_adapter_factory("daos", factory)`. 내장 어댑터는 pkgutil 지연 로딩이고, **외부 어댑터는 서버 인자 파싱 전에 import 돼 있어야** 한다(`_L2_ADAPTER_CONFIG_REGISTRY[type]` 조회) → 우리 엔트리포인트가 필요 |
| 계약 | `L2AdapterInterface` 추상 메서드 9 개: `get_{store,lookup_and_lock,load}_event_fd`, `submit_store_task(keys, objects)→task_id`, `pop_completed_store_tasks()→{id: L2StoreResult}`, `submit_lookup_and_lock_task(keys, layout_desc)`, `query_lookup_and_lock_result(id)→Bitmap|None`(한 번만 non-None), `submit_unlock(keys)`(실패 불허), `submit_load_task(keys, objects)`, `query_load_result(id)→Bitmap|None`. 선택: `delete(keys)`, `report_status()`, `close()`, 리스너(`on_l2_keys_stored`). **두 컨트롤러 스레드(store/prefetch)에서 동시에 호출됨 → 스레드 세이프 필수** |
| 버퍼 | store/load 모두 **호출자가 준 `MemoryObj`** 를 쓴다. `obj.byte_array` 는 pinned L1 의 `memoryview` → **DAOS 읽기를 L1 버퍼에 직접(zero-copy) 할 수 있다.** 어댑터는 MemoryObj 수명을 관리하지 않는다 |
| 키 | `ObjectKey(chunk_hash: bytes, model_name, kv_rank, object_group_id, cache_salt)`. `@` 는 필드에 못 들어옴(구분자로 예약) |
| 참고 구현 | `fs_l2_adapter.py`(764 줄: asyncio 루프 스레드, O_DIRECT, tmp+rename, 파일=raw 바이트), `native_connector_l2_adapter.py`(536 줄: 클라이언트측 refcount 락, eventfd 3 개 + demux 스레드) |
| 전송 | `lmcache_driven`(서버가 CUDA IPC 로 GPU↔L1 직접) 또는 `engine_driven`(PREPARE/COMMIT, SHM). L2 load 완료는 **태스크 단위 비트맵**으로 통지되고 서버가 L1→GPU 를 이어 간다 |

## 2. 이 작업이 만드는 것

```
lmcache_daos/mp/
  __init__.py
  l2_adapter.py     DaosL2AdapterConfig(L2AdapterConfigBase) + DaosL2Adapter(L2AdapterInterface) + 등록
  server.py         엔트리포인트: 어댑터 등록 → lmcache.v1.multiprocess.server 의 main 에 위임
deploy/launchers/run_vllm_mp_c5.sh   컨테이너 하나에서 MP 서버 + vLLM(LMCacheMPConnector) 기동
tests/mp/           단위(모의 DfsSys) + 통합(client-5 gate/bench)
doc/MP-MODE-PLAN.md (이 문서) → 결과는 deploy/README.md §11 로
```

### 2.1 설정 (`--l2-adapter` JSON)

```json
{"type":"daos","pool":"attr1","container":"kvlmc5","root":"/mp",
 "workers":8,"max_capacity_gb":0,"verify_size":true}
```

`pool/container` 는 기존 커넥터의 `plugin://daos/<pool>/<cont>` 와 같은 대상이고, `root` 로 in-process
객체(`/` 아래)와 네임스페이스를 분리한다(키 형식이 달라 어차피 공유 불가).

### 2.2 키 → DFS 경로

`<root>/<model_name(슬래시→'_')>@<kv_rank:08x>@<object_group_id:x>@<chunk_hash.hex()>[@<cache_salt>]`
— native/fs 어댑터와 같은 직렬화, 단일 디렉터리(fanout 은 이전 측정에서 불필요로 결론).

### 2.3 파일 프레이밍

fs 어댑터처럼 **raw 페이로드만** 저장한다. 레이아웃은 호출자(L1)가 이미 알고 있고, 절단 판정은
`읽은 바이트 == len(byte_array)` 로 한다(`verify_size`). 기존 `[prefix 8B][meta][payload]` 는 in-process
전용으로 남긴다. 쓰기는 `tmp 이름 → dfs_sys_rename`(부분 저장 노출 방지, 기존 T4 교훈).

### 2.4 실행 모델

- 태스크 큐 + 고정 스레드풀(`workers`). 각 태스크는 키 배치를 병렬 처리하고 결과를 `dict[task_id]` 에
  넣은 뒤 해당 eventfd 에 `notify()`. eventfd 는 `lmcache.v1.platform.create_event_notifier()` 3 개.
- **lookup_and_lock**: `dfs_sys_stat` 병렬 + 클라이언트측 refcount(`dict[ObjectKey,int]`) — DAOS 는
  어댑터 밖에서 지우지 않으니 락은 "우리 delete 가 건너뛰게 하는" 용도. `submit_unlock` 은 감소만.
- **load**: `byte_array` memoryview → `ctypes.c_char.from_buffer()` 주소로 `dfs_sys_read` 직접 수행
  (L1 pinned 버퍼로 zero-copy). 크기 불일치·오류는 비트맵 0.
- **store**: 존재하면 건너뜀(idempotent), 아니면 tmp 쓰기 → rename. 결과 `L2StoreResult(success, bytes)`.
  성공 시 리스너 `on_l2_keys_stored` 호출, 사용량 카운터(`_bytes_by_cache_salt`) 갱신.
- **delete**: `remove_type` 경로 재사용(락된 키는 건너뜀). **report_status**: 큐 깊이, 완료 수, 오류 수.
- 오류 처리는 계약대로: store 는 태스크 단위, lookup/load 는 키 단위 비트맵.

### 2.5 스트리밍 GET 과의 관계 (이 작업의 부가 가치)

in-process 에서 `streaming.py` 가 노렸던 "completion-ordered read ⊕ H2D 겹침" 은 MP 구조에서는
**서버가 L2 load 완료를 태스크 단위로 받아 L1→GPU 를 이어 가므로 구조적으로 가능**하다. 어댑터가
큰 배치를 객체별 소태스크로 쪼개 완료 통지를 잘게 내면 상류 패치 없이 겹침이 생기는지 실측한다.
(확정 주장 아님 — Phase 3 의 측정 항목.)

## 3. 단계

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| 0 | 골격: config/adapter 클래스, 등록, `lmcache_daos.mp.server` 엔트리. `--l2-adapter '{"type":"daos",…}'` 로 서버가 기동되고 `report_status` 가 나옴 | 서버 기동 + `/health` |
| 1 | store/lookup/load/unlock/delete 구현, 스레드풀·eventfd·비트맵. 모의 `DfsSys` 단위 테스트(정상·절단·오류·동시 호출) | `pytest tests/mp` 통과 |
| 2 | client-5 통합: 컨테이너 안에서 MP 서버 + vLLM(`LMCacheMPConnector`, `--ipc host`, CUDA IPC). `kv_correctness_gate.sh` + `bench_value` 4K/8K/16K | 게이트 PASS, in-process 대비 수치 표 |
| 3 | 최적화·측정: 소태스크 분할로 스트리밍 겹침 실측, `workers` 스윕, 필요하면 `daos_event.py` 의 EQ 비동기 경로 적용, 2 개 vLLM 인스턴스가 한 MP 서버 L1 을 공유하는 시나리오 | deploy/README §11 |

비교 기준(in-process, 2026-09-03, Qwen3-14B/32K, ucx+rc_v, c_ops): hit TTFT 4K 103–127 ms, 8K 151–221 ms,
16K 251–444 ms.

## 4. 배포 형태 (client-5)

한 podman 컨테이너(`kvsup-ucx-lmc:local`, `--ipc host`, GPU, `/daoslib`)에서 두 프로세스:

```
python -m lmcache_daos.mp.server --host tcp://localhost --port 5555 --chunk-size 256 \
  --l1-size-gb 100 --eviction-policy LRU --max-gpu-workers 4 --max-cpu-workers 8 \
  --l2-adapter '{"type":"daos","pool":"attr1","container":"kvlmc5","root":"/mp","workers":8}' &
vllm serve … --kv-transfer-config '{"kv_connector":"LMCacheMPConnector","kv_role":"kv_both",
  "kv_connector_extra_config":{"lmcache.mp.host":"tcp://localhost","lmcache.mp.port":5555}}'
```

`LMCACHE_CONFIG_FILE` 은 MP 모드에서 쓰지 않는다(서버 인자가 설정).

## 5. 리스크·미확인 (구현 중 확인)

1. `MemoryObj.byte_array` 가 반환하는 memoryview 가 모든 L1 객체 타입에서 쓰기 가능·연속인지
   (`memory_management.py` 856 행 구현체 기준으로 확인).
2. `L2AdapterConfigBase.from_dict/help` 필수 시그니처(`fs_l2_adapter.FSL2AdapterConfig` 를 그대로 따름).
3. `pop_completed_store_tasks` 의 리스너 호출 시점(store 컨트롤러 스레드에서 호출되는지).
4. ObjectKey 의 `object_group_id`/`kv_rank`: TP=1·단일 그룹이면 각각 하나. hybrid 모델은 범위 밖.
5. LMCache 0.5.2 의 `Double free` 경고가 MP 경로에도 있는지(관찰만).
6. 컨테이너 안 두 프로세스 관리: 런처가 MP 서버 준비(`/health` 또는 포트)를 확인한 뒤 vLLM 기동.
