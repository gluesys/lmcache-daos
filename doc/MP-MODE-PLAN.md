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

## 7.7 스트리밍 프로토타입 — L2 load 와 L1→GPU 전송 겹치기 (2026-09-05)

§7.2 대로 LMCache MP 서버는 요청당 load 태스크 하나가 끝난 뒤에야 vLLM 이 스케줄하고 GPU 전송을 시작한다.
설치된 LMCache 를 고치지 않고 우리 서버 엔트리에서 네 지점을 몽키패치했다(`lmcache_daos/mp/streaming_patch.py`,
`DAOS_MP_STREAM=<소배치 키 수>`): ① 어댑터 프록시가 load 를 소배치로 나눠 완료분을 즉시 L1 읽기 가능 상태로 전환,
② 프리페치 상태 질의가 lookup 이 끝난 시점에 hit 수를 돌려줌(vLLM 이 load 중에 스케줄), ③ `read_prefetched_results` 가
미도착 키를 자리표시자로 넘김, ④ H2D 전송 함수가 소배치 단위로 도착을 기다리며(조건변수) 전송.

정합성: 콜드 hit 의 생성 텍스트가 recompute 참조와 일치, 게이트 PASS.

| ctx | KV | L1 warm | **콜드 no-stream** | **콜드 stream=1** | stream=4 | stream=8 | 개선 | 이론 하한(50 ms + KV/34 GB/s) |
|---|---|---|---|---|---|---|---|---|
| 16K | 2.68 GB | 88 | 173 | 218 | 200 | 121 | 잡음 수준 | 129 |
| 31K | 5.20 | 142 | 313 | **213** | 211 | 216 | **1.47×** | 203 |
| 64K | 10.74 | 319 | 712 | **406** | 404 | 410 | **1.76×** | 366 |
| 127K | 21.43 | 554 | 1183 | **808** | 811 | 822 | **1.46×** | 680 |

(정상 상태 값 = 재시작 후 두 번째 요청. 첫 요청은 §7.4 스톨과 워밍업이 섞여 제외. 소배치 크기는 1~8 사이에서 차이 없음.)

31K 는 이론 하한(load 시간 + 고정 오버헤드)에 도달했고 64K·127K 는 하한의 ~90%다. 즉 **콜드 hit 가 DAOS 읽기 시간으로
수렴**했다 — Hub 문서 §7-4a 가 "남은 유일한 레버" 라고 한 겹침이 MP 구조에서 실제로 성립한다. 남는 것은 DAOS 대역폭 자체다.

처음 두 판은 실패였고 원인이 교훈이다: `_resolve` 가 0.5 ms 폴링으로 L1 을 확인하자 GIL 경합으로 load 가 느려져 이득이
사라졌다(조건변수로 교체해 해결). 또 `DAOS_MP_STREAM=0` 을 "비활성"으로 처리하지 않아 기준선 두 세션이 실제로는
소배치 1 스트리밍이었다(수정, `int(x) > 0` 만 활성).

한계(프로토타입): 조기 `finish_write_and_reserve_read` 가 `extra_count=0`(TP=1 전용), 나중에 load 가 실패한 키는 retrieve
실패로 표면화, 실제 프리페치 결과는 janitor 스레드가 회수. **상류 제안 형태**: 프리페치 컨트롤러가 요청 키를 소배치 태스크로
나누고 완료분부터 `finish_write_and_reserve_read`, 프리페치 상태를 lookup 완료 시점에 공개, `retrieve()` 가 키 단위로 대기 —
이 네 지점 그대로다.

### 7.7a §7.4 스톨 재발 — 프로브로는 막히지 않았다
16 청크 프로브(전 타깃)를 넣은 뒤에도 재시작 8 회 중 4 회, **첫 대용량 load**(64K·127K 첫 요청)가 14.9~16.2 s 걸렸다.
프로브(bytearray 로 읽기)와 워밍업 store 는 무관하다는 뜻이다. 새 가설: **pinned L1(100 GB, 10 GB 세그먼트 10 개)의
첫 RDMA 메모리 등록**. 첫 load 만 L1 버퍼로 읽고, L1=20 GB 세션들에서는 스톨을 한 번도 보지 못했으며, 동시 소태스크가
전부 같은 시간 멈춘 것(등록 락)과 맞는다. 검증은 `UCX_IB_REG_METHODS=odp`(pinning 없는 등록) A/B — §7.7b.

### 7.7b 첫-로드 15 s 스톨 — 원인 국소화 (2026-09-05, 미해결)

재현 조건이 확정됐다: **같은 프로세스에서 store(쓰기) 직후 몇 초 안에 대용량 load 를 하면** 그 load 의 모든 동시 read 가
14.4~16.2 s 멈춘다. load 만 하면 0/10, store→load 는 12/16. store 뒤 12 s 를 두면 스톨이 ~3.5 s(= 15 − 12)로 줄고 30 s 뒤에는
없다 → **쓰기 시각 기준 약 15 s 에 풀리는 타이머성** 현상이다. 스레드 수(2/8/16), L1 크기(20/100), `--no-l1-use-lazy`, 서버 GC,
클라이언트 GC, UCX `IB_REG_METHODS=odp`, 풀 속성 `checkpoint:disabled`·`reclaim:disabled` 모두 무효(각 3 회 이상).

계층별 관측:
- 클라이언트(DAOS object/dtx/rpc DEBUG): fetch RPC 들을 `submitted` 로 찍은 뒤 **13 s 동안 로그 0 줄**(재시도·타임아웃·INPROGRESS 없음),
  그 뒤 완료가 몰려온다. perf 는 그 동안 DAOS 클라이언트 진행 엔진(`tse_sched_progress`/`hg_core_progress`)이 futex 스핀락에서
  CPU 54% 를 태우는 것을 보인다(결과이지 원인은 아님 — 스레드 2 개에서도 스톨).
- 서버(cell1/cell2, RPC/CRT/HG DEBUG): 같은 창에 **클라이언트 RPC 수신이 0 건**(랭크 간 SWIM 5 s 주기만), fetch RPC 는
  스톨이 끝나는 시각에 도착해 즉시 처리된다. VOS/DTX/BIO 경고 없음. 세 호스트 시계는 NTP 동기(µs).

⇒ RPC 가 **클라이언트의 mercury/UCX 송신 경로에 ~14 s 머문다.** 서버·DAOS 서버 계층은 무죄다. 유력 후보는 store 의 bulk
GET(서버가 클라이언트 pinned L1 을 RDMA read) 뒤 UCX 엔드포인트/플로우컨트롤 상태가 다음 송신을 막고 ~15 s 타임아웃으로
회복되는 것. 클라이언트 mercury 로그(`HG_LOG_LEVEL`)는 이 빌드에서 잡히지 않아 여기서 멈춘다. 프로브(bytearray 쓰기→읽기)도
1/15 스톨했으므로 pinned 메모리 한정은 아니다.

**운영 함의**: 콜드 KV 를 저장한 직후 수 초 안에 다른 대용량 KV 를 읽는 패턴(populate 직후 질의)에서 첫 요청이 15 s 걸릴 수 있다.
in-process 커넥터는 같은 라이브러리를 쓰므로 원리상 같은 노출이 있다(측정은 MP 에서만). 다음 단계는 mercury NA-UCX 로그를
빌드에서 켜거나 `ofi+verbs`/`ofi+tcp` 로 전송을 바꿔 UCX 한정인지 가르는 것(서버 재포맷 필요).

### 7.7c 전송 A/B — 스톨은 **UCX(ucx+rc_v) 한정**이다 (2026-09-05)

클러스터를 `ofi+tcp` 로 재포맷하고(양 cell provider 교체, client-5 agent 도메인 `ens255np0`) 같은 트리거로 재시도했다.

| 전송 | store→load 첫 load | load only |
|---|---|---|
| `ucx+rc_v` | **12/16 스톨**, 14.4~16.2 s | 0/10 |
| `ofi+tcp` | **0/8 스톨**, 460~474 ms 매번 | 0/2 |

같은 어댑터·같은 워크로드·같은 서버에서 전송만 바꿔 사라졌으므로 원인은 **mercury NA-UCX / UCX 계층**이다(§7.7b 의 "RPC 가
클라이언트 송신 경로에 머문다" 와 일치). 유력 기전은 store 의 bulk GET 이 끝난 뒤 UCX 엔드포인트가 다음 송신을 막고 ~15 s
타임아웃으로 회복되는 것이며, 상류(mercury/UCX 또는 DAOS `crt`) 이슈로 올릴 재현 절차는 §7.7b 의 store→load 다.

tcp 의 대역폭은 8K(2.7 GB) load 466 ms = 어댑터 3.7 GB/s 로 UCX 의 ~34 GB/s 대비 1/9 이므로 운영 대안은 아니다. 측정 뒤 클러스터는
`ucx+rc_v` 로 되돌렸다. 운영 회피책은 "store 직후 15 s 안에 같은 프로세스에서 콜드 load 가 오지 않게" 하는 것인데 실사용에서는
보장할 수 없으므로 상류 수정이 답이다. 후보 회피책(미검증): `ofi+verbs` 로 RDMA 유지(§0 의 rxm 손상 이력은 공유 드라이브 오구성
때문이었을 가능성이 높아 재평가 가치 있음).

### 7.7d `ofi+verbs;ofi_rxm` — 스톨 없음, 대역폭 동일 → **권고 전송** (2026-09-05)

| 전송 | store→load 첫 load(8K) | load only | 어댑터 읽기 | 8K 콜드 hit | 정합성 |
|---|---|---|---|---|---|
| `ucx+rc_v` | 12/16 스톨 | 0/10 | 33~37 GB/s | ~150 ms | 게이트 PASS |
| `ofi+tcp` | 0/8 | 0/2 | 3.7 GB/s | 576 ms | — |
| **`ofi+verbs;ofi_rxm`** | **0/6, 46~49 ms** | 0/2 | **35~38 GB/s** | **153~157 ms** | 게이트 6/6 PASS, raw 0/320 + 감사 0/32 |

verbs 는 UCX 와 같은 대역폭(16K 콜드 289 ms, load 97~102 ms = 36~38 GB/s)에 스톨이 없다. 클러스터는 **verbs 로 유지**한다.

이 결과는 이 프로젝트 초기의 판정 — "libfabric `verbs;ofi_rxm` 은 대용량 RDMA read 를 조용히 손상시킨다(28 MB × 30 중
3~10 개만 정상), UCX 로 해결" (`deploy/README.md` §2, Hub 문서) — 를 **뒤집는다.** 그때의 손상은 두 랭크가 같은 NVMe 를 쓰던
오구성(`gpudirect/DAOS-CONCURRENT-READ-CORRUPTION.md` §62)이었고, UCX 가 "고친" 것처럼 보인 것은 30 회 시행의 검정력 부족이었다.
분리된 드라이브 위에서 verbs 는 raw 320 회 + 정지 감사 32 객체 전부 정상이다.

### 7.7e 스톨 근본 원인 — 지연 연결(rdma_cm) 의 RTU 유실 + 커널 CM 재전송 타이머 (2026-09-05)

`ucx+rc_v` 로 되돌려 클라이언트 cart DEBUG(rpc/hg), 서버 xstream DEBUG, 양쪽 HCA 하드웨어 카운터, 커널 CM(connection
manager) 카운터를 동시에 잡았다(6/6 재현). 결론: **store 중에 처음 접촉하는 서버 xstream 으로의 rdma_cm 연결 수립에서
클라이언트→서버 `RTU` 가 유실되고, 서버 커널 CM 이 `REP` 를 재전송하는 ~16 s 동안 그 rank:tag 로 가는 모든 RPC 가 대기한다.**

증거(run "ucxlog 2", 스톨 14.5 s, 막힌 엔드포인트 rank 0 tag 9):
1. 클라이언트 cart 로그: rank 0 tag 9 로의 **첫 URI lookup·첫 송신이 05:44:23.22, warm store 도중**이다. 기동 시 프로브는
   14 개 엔드포인트만 연결했고(S16 16 청크가 12 타깃에만 놓임) 0:5, 0:9, 1:3, 1:5 는 store 가 처음 접촉했다.
   막힌 RPC 는 전부 0:9 행(update 7 + fetch 3), 다른 rank:tag 는 즉시 완료. 05:44:39.66 에 전부 동시 완료.
2. 서버 xs 9(cell1): 05:44:24.70 이후 로그 0 줄(유휴), 막힌 update 를 **05:44:39.663 에 수신**해 7 ms 안에 응답.
3. HCA 카운터(양쪽): 스톨 창에 ack timeout·RNR·retrans 증가 0 → 와이어 재전송 없음, 패킷이 QP 에 오르지 않았다.
4. **CM 카운터**: 클라이언트 `cm_tx_msgs/req` +4 (store 중 새 연결 4 개), 서버 cell1 14:44:23~24 `rep +2, rx_req +2, rx_rtu +1`
   → REQ 2 개 중 **RTU 하나가 도착하지 않음**. 14:44:40 `retry_rep +1`(서버 REP 재전송) → 클라이언트 `dup_rep +1, retry_rtu +1`
   (중복 REP 수신, RTU 재송신) → 연결 성립 → 스톨 종료. keepalive(3 s)·FC off 는 무효(§7.7d 이후 실험).
5. RTU 가 왜 store 중에만 유실되는가: store 는 서버가 클라이언트 메모리를 RDMA read 하므로 **클라이언트→서버 방향이 포화**된다.
   이 패브릭은 PFC 없음(client-5 `rx_discards_phy` 2.1 M, 서버 `packet_seq_err` 4 만, ECN CNP 200 만): 손실형 RoCE 에서 작은 CM
   MAD 가 함께 버려진다. load 는 서버→클라이언트 방향이 포화라 RTU(클→서) 는 살아남는다 — load-only 0/12 와 일치.
6. 왜 UCX 한정인가(추정): libfabric verbs 는 rdma_cm 이 만든 QP 를 쓰므로 RTU 가 없어도 첫 데이터 패킷이 커널 CM 의
   `COMM_EST` 로 연결을 확정한다. UCX rdmacm 은 QP 를 자체 관리해 `ESTABLISHED`(RTU) 이벤트를 기다린다.

**수정**: 어댑터 기동 프로브를 **SX(모든 타깃에 샤드 1 개) 오브젝트 클래스**로 만들고 `probe_chunks`(기본 64) 청크를 써서
읽는다(`lmcache_daos/mp/l2_adapter.py::_warm_up`, `dfs_binding.open_rdwr_create(oclass=)`). 모든 rank:tag 연결이 요청을 받기
전, 패브릭이 조용할 때 성립하므로 사용자 트래픽 중 첫 접촉이 사라진다. 프로브는 프로세스별 이름으로 만들고 지운다.
남는 노출은 기동 시 연결 자체가 RTU 유실을 겪는 경우(기동 지연 16 s, 요청 경로 아님)와 서버 재시작 후 재연결이다.
근본 해결은 패브릭 측(PFC/무손실 클래스 또는 CM MAD 우선순위)이고, DAOS/UCX 상류에는 "NA-UCX 지연 연결이 손실 패브릭에서
RTU 유실 시 16 s 멈춤" 으로 보고할 수 있다.

**검증(`ucx+rc_v`, 2026-09-05)**: 수정 후 store→load **10/10 스톨 없음**(콜드 hit 159~173 ms, 첫 load 52~68 ms). CM 카운터로 기동 시
연결 18 개(2 rank × 8 target + tag 0 × 2)가 전부 성립하고 store·load 중 새 연결 0, RTU 재송 0 을 확인했다. 첫 구현(256 MiB 병렬
쓰기로 접촉)은 RTU 유실이 기동 단계로 옮겨가 6 회 중 5 회 워밍업이 17 s 걸렸다 → 접촉 단계를 "청크당 4 KiB 순차 쓰기"(64 타깃
130 ms)로 분리하고 그 뒤에 4 MiB 병렬 쓰기·읽기를 하도록 고쳐 워밍업 총 350~450 ms. 단위 테스트 PASS.
전송 선택: 수정 뒤에는 UCX 도 스톨이 없지만 어댑터 읽기 대역폭은 verbs 가 높으므로(39~40 vs 26~33 GB/s) 권고 구성은 §7.7d 대로
`ofi+verbs;ofi_rxm` 유지. 클러스터는 검증 후 verbs 로 되돌렸다.
