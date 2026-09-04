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

## 6. 결과 (2026-09-04, Phase 0~2 완료)

구현: `lmcache_daos/mp/l2_adapter.py`(어댑터 + `type: "daos"` 등록), `lmcache_daos/mp/server.py`(엔트리),
`lmcache_daos/mp/vllm_connector.py`(vLLM 측 셈), `dfs_binding.py` 에 `stat_size`/`mkdir_p` 추가,
`tests/mp/test_l2_adapter.py`(모의 DfsSys, 4 케이스 PASS), `deploy/launchers/run_vllm_mp_c5.sh`,
`bench/bench_persist.py`(콜드 L1 측정), 게이트에 `HIT_PATTERN`.

### 6.1 구현 중 확인된 것 (§5 의 미확인 항목 해소)

| 항목 | 결과 |
|---|---|
| 외부 어댑터 등록 | 서버 인자 파싱 전에 import 필요 → `python -m lmcache_daos.mp.server` 엔트리. `--help` 의 타입 목록에 `daos` 가 나옴 |
| `byte_array` | pinned L1 객체의 쓰기 가능한 memoryview. `(c_char*n).from_buffer()` 로 zero-copy 읽기·쓰기 동작 |
| 스레드풀 | 태스크 풀과 I/O 풀을 **분리**해야 한다. 한 풀에서 태스크가 하위 I/O 를 기다리면 워커 수만큼의 동시 태스크에서 자기교착(단위 테스트에서 재현) |
| ObjectKey | `cache_salt` 에도 `/` 금지. model_name 은 vLLM 이 넘긴 **모델 경로 전체**(`/hf/hub/…/snapshots/<hash>`)라 `/`→`_` 접기가 실제로 필요 |
| 저장 단위 | 청크 256 토큰 × 160 KiB = **40 MiB/객체**. `/mp` 에 564 객체 적재 확인 |
| L2 쓰기 정책 | 기본 정책에서 store 가 L2 로도 곧바로 내려간다(write-through). 재시작 뒤 L1 이 비어도 DAOS 에서 `0 L1, 21 L2` 로 채워짐 |
| `--disable-observability` | 이미지에 `opentelemetry.exporter.prometheus` 가 없어 필수 |
| vLLM 0.18 ↔ LMCache 0.5.2 스큐 | vLLM 내장 `LMCacheMPConnector` 와 LMCache 의 `_0180` 변형 모두 서버 URL 을 **문자열**로 넘기는데 어댑터는 리스트를 받는다(`ZMQError addr='t'`). 일반 변형은 `KVCacheSpecKind` 로 vLLM ≥0.19 를 요구. 셈이 `_0180` 을 고르고 `LMCacheMPSchedulerAdapter.__init__` 의 URL 을 리스트로 강제 |
| vLLM factory | 등록된 이름을 `kv_connector_module_path` 보다 먼저 해석 → 미등록 이름 `DaosMPConnector` 로 우회 |

### 6.2 측정 (client-5, Qwen3-14B/32K, ucx+rc_v, c_ops, 서버 `--l1-size-gb 100`)

정합성 게이트: **PASS 6/6** (`HIT_PATTERN` MP 패턴).

| 프롬프트 | miss(recompute) | MP warm hit (L1) | **MP cold hit (DAOS L2→L1→GPU)** | in-process hit (2026-09-03) |
|---|---|---|---|---|
| ~4K tok | 364~373 ms | 50~61 ms | **189 ms**(재시작 직후 1회) | 103~127 ms |
| ~8K tok | 779~787 ms | 85~95 ms | **163 ms** | 151~221 ms |
| ~16K tok | 1931~2045 ms | 126~135 ms | **275 ms** | 251~444 ms |

- 콜드 hit 는 `bench_persist.py` 로 저장 → 컨테이너 재시작(L1 소거) → 같은 프롬프트 재요청. 서버 로그
  `Prefetch request completed: 21/21 retained keys (0 L1, 21 L2) in 91.4 ms`(840 MiB → 약 9 GB/s) 가 DAOS 경로임을 증명.
- 4K 콜드의 189 ms 는 재시작 후 첫 요청의 일회성 비용을 포함한다(이후 같은 크기의 8K 가 163 ms).
- **MP 의 DAOS 경로는 in-process 와 같거나 빠르고**(16K 275 vs 251~444), 반복 hit 는 L1 에서 50~135 ms 로 끝난다.
  §2.5 의 스트리밍 겹침 가설은 아직 분리 측정하지 않았다(Phase 3).

### 6.3 남은 것 (Phase 3)
1. 소태스크 분할로 L2 load 완료를 잘게 통지해 L2→L1→GPU 겹침이 생기는지 실측. `workers` 스윕.
2. `report_status` 를 볼 경로(HTTP 프런트 또는 주기 로그).
3. 두 vLLM 인스턴스가 한 MP 서버를 공유하는 시나리오.
4. LMCache `Double free` 경고가 MP 경로에도 나는지 확인.

## 7. Phase 3 결과 (2026-09-04 저녁)

### 7.1 어댑터 읽기 대역폭은 DAOS 상한에 있다 — `workers` 는 무관

`bench_persist.py` 로 저장 → 재시작 → 콜드 hit, 태그 3 개 × 크기 2 종, `workers` ∈ {8, 16, 32}.
어댑터가 태스크마다 남기는 `daos l2 load task: N keys, MiB, ms, GB/s` 로그 기준:

| 배치 | 크기 | workers 8 | 16 | 32 |
|---|---|---|---|---|
| 8K 프롬프트 (41 키) | 1.64 GiB | 50~55 ms, 31~35 GB/s | 49~52 ms, 33~35 GB/s | 46~52 ms, 33~37 GB/s |
| 16K 프롬프트 (88 키) | 3.52 GiB | 104~119 ms, 31~36 GB/s | 101~105 ms, 35~37 GB/s | 100~103 ms, 36~37 GB/s |
| 콜드 hit TTFT 8K / 16K | | 150~151 / 248~267 ms | 150~151 / 245~331 ms | 148~160 / 242~250 ms |

DAOS→pinned L1 이 **33~37 GB/s** 로, in-process 커넥터가 raw read 로 재던 상한(34 GB/s)과 같다. 기본 8 로 둔다.
프로세스 기동 후 **첫** load 태스크만 15~16 GB/s(연결 워밍업)이고 두 번째부터 상한이다.

### 7.2 겹침(§2.5) 은 어댑터 수준에서 불가 — 서버가 요청당 load 태스크 하나를 낸다

`prefetch_controller.py` Step 7: 요청의 L2 키 전부를 **어댑터당 하나의 `submit_load_task`** 로 제출하고 그
task_id 의 비트맵을 기다린 뒤 L1→GPU 를 시작한다. 완료 통지가 task_id 단위라 어댑터가 내부를 아무리 쪼개도
서버는 배치 전체가 끝나야 움직인다. 실측도 그렇다: 콜드 hit − warm(L1) hit ≈ DAOS load 시간
(8K: 150−90 ≈ 50 ms, 16K: 250−130 ≈ 105 ms) 로 **직렬**이다. 겹침은 LMCache 서버(프리페치 컨트롤러가
키를 여러 태스크로 나누고 도착분부터 전송)에서만 가능하다 → 상류 제안 후보. 어댑터 쪽 레버는 대역폭만이고 그건 이미 상한이다.

### 7.3 두 vLLM 인스턴스가 한 MP 서버를 공유 (`launchers/run_vllm_mp2_c5.sh`)

Qwen3-14B 두 인스턴스(8001/8002, GPU util 0.42 씩) + MP 서버 하나. A 에 저장한 프롬프트를 B 가 처음 봤을 때:

| | A 저장 miss | **B 첫 요청** | B 자체 recompute |
|---|---|---|---|
| 8K | 935 ms | **125 ms** (`41 L1, 0 L2`) | 786 ms |
| 16K | 1930 ms | **140 ms** (`88 L1, 0 L2`) | ~1.9 s |

재시작 뒤 B 가 A 가 저장한 KV 를 **DAOS 에서** 264 ms(16K)로 받았고, B 에서 게이트 PASS 4/4. in-process 커넥터로는
불가능한 성질(인스턴스 간 L1 공유)이 실측으로 확인됐다.

### 7.4 미해결 — 프로세스 기동 후 첫 DAOS I/O 가 간헐적으로 14~17 초

재시작 약 9 회 중 3 회, 첫 DAOS 작업(store 든 load 든)이 14.4 / 15.3 / 17.1 초 걸렸다(예: `load task 3: 1640 MiB in
14412.6 ms`). 그 뒤 작업은 정상. 서버(cell1/cell2) 로그에 같은 시각 ERR/WARN 없음, `D_LOG_MASK=WARN` 클라이언트 로그를
켠 3 회 재시도에서는 재현되지 않아 원인 미확정(전송 엔드포인트 설정/재시도 의심). **완화**: 어댑터 생성 시
4 KiB 프로브 객체 `<root>/.daos-l2-probe` 를 쓰고 읽어 첫 I/O 비용을 기동 시점으로 옮겼다(정상 시 23 ms).
스톨이 이 프로브에서 나면 서버 기동이 15 초 늦어지는 것으로 끝나고 사용자 요청은 맞지 않는다.

### 7.5 기타
- MP 경로에서는 LMCache 의 `MemoryObj ref count negative / Double free` 경고가 **0 건**이다(in-process 는 수백 건).
- `report_status` 는 서버의 `--enable-extra-logging` 이 observability(이미지에 없는 prometheus exporter)를 요구해 못 쓰고,
  어댑터 옵션 `status_interval_s` 로 활동이 있을 때만 주기 로그를 낸다.
- 런처에 `D_LOG_MASK`/`D_LOG_FILE`(기본 WARN, `/tmp/daos_client.log`)을 넘긴다.

## 7.6 L1=20 GB 의 p95 꼬리 — 원인은 포화 큐잉, 레버는 동시성 (2026-09-05)

Part B(100 GB working set, 12 inflight)에서 L1=20 GB 일 때 p95 가 avg 의 2~3 배(909 ms 관측)였다.
`longdocqa.py DUMP=1` 로 요청별 지연을 시작 순서로 찍어 보면 **>400 ms 요청이 매 런 같은 위치(79~90, 127~138)에
12 개씩 뭉쳐** 나온다. 설정을 바꿔도 위치가 그대로다.

### 배제한 것 (전부 같은 하네스, 질의만, 런마다 재시작)

| 가설 | 시험 | 결과 |
|---|---|---|
| 서버 프리페치 동시 한도(기본 8 < 12) | `--l2-prefetch-max-in-flight 16` | 변화 없음 |
| L1 eviction 시점 | watermark 0.5·0.6 / ratio 0.3, `--l2-store-policy skip_l1`, eviction tick 1 s → 0.05 s(`DAOS_MP_EVICT_TICK_S`) | 변화 없음. `reserve_write` OUT_OF_MEMORY **0 건**(`DAOS_MP_TRACE=1`). L1=100 에서도 같은 위치의 웨이브 |
| L1 lazy pinned 확장 | `--no-l1-use-lazy` | 변화 없음(확장 로그 0) |
| 어댑터 태스크 슬롯 8 개 | `task_workers` 32 | 변화 없음 |
| 어댑터 I/O 스레드 | `workers` 16 / GPU 워커 8·12 / CPU 워커 16 | 변화 없음 |
| 클라이언트 GC / 서버 GC | `NOGC=1` / `DAOS_MP_GC=freeze` | 변화 없음 |
| lookup 이 bulk 읽기 뒤에 줄 섬 | 메타데이터 전용 풀 | **avg 290→270 ms, 집계 26→29 GB/s** 개선. p95 는 그대로 |
| L2 단계의 FIFO 계단(태스크 25→230 ms) | `load_schedule: fair`(태스크 간 키 라운드로빈) | L2 단계는 균등화(80~120 ms)됐지만 **요청 p95 불변** → 꼬리는 L2 단계 밖 |
| `--l2-prefetch-policy retain` | | **훨씬 악화**(7.9 GB/s) — L1 < working set 에서 금지 |

서버 로그로 단계별 시간을 재면 어댑터 load 는 34~130 ms, 프리페치 완료→GPU 전송 지연 p95 21 ms 로 모두 짧다.
꼬리 요청의 초과 시간은 어느 한 단계가 아니라 **한 웨이브를 통째로 기다린 것**이다.

### 확정 — 대역폭 포화에서의 배치 큐잉

12 inflight × 640 MB 를 DAOS 상한 ~30 GB/s 로 나누면 웨이브 하나가 ~230 ms 다. p50(235) 은 웨이브 하나,
p95(≈460) 는 웨이브 도중 도착해 다음 웨이브까지 기다린 요청이다. Little 의 법칙상 12 inflight 의 평균은
12 × 0.64 GB / 30 GB/s ≈ 256 ms 아래로 내려갈 수 없고(실측 avg 271), 스케줄링은 분포 모양만 바꿀 수 있다.

| inflight | avg | p50 | p95 | p95/p50 | 집계 |
|---|---|---|---|---|---|
| **6** | **132 ms** | 125 | **152** | **1.22** | **30.0 GB/s** |
| 12 | 271 | 235 | 456 | 1.94 | 29.0 |
| 24 | 609 | 465 | 1231 | 2.56 | 25.1 |

**inflight 6 은 같은 처리량(30 GB/s)을 내면서 p95 가 152 ms 다.** 12 는 처리량을 더 얻지 못하고 큐만 늘린다.
서버 쪽에서 `--l2-prefetch-max-in-flight 4·6` 으로 admission 을 제한해도 대기가 큐로 옮겨갈 뿐 p95 는 그대로다
(465~475) — 대기 위치가 어디든 12 개가 들어오면 12 개 분의 시간이 걸린다.

### 권고
1. **MP 서버(= DAOS 대역폭 단위)당 동시 KV 요청 수를 대역폭에 맞춘다.** 기준: `inflight ≈ 목표 TTFT × BW / KV_per_req`
   → 30 GB/s, 640 MB, 150 ms 이면 ~6~7. vLLM 쪽 `--max-num-seqs` 또는 앞단 admission 으로 건다.
2. 처리량을 더 원하면 랭크(대역폭)를 늘린다 — 12 inflight 에서도 집계는 이미 상한이다.
3. L1 hit 비율이 진짜 레버다(L1 상주 시 avg 202 / p95 236). L1 < working set 이면 `retain` 은 켜지 말 것.
4. 어댑터: 메타데이터 풀(기본 on)은 유지. `load_schedule: fair` 는 기본 fifo 로 둔다(끝단 이득 없음).
