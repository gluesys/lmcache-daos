# lmcache-daos

LMCache의 KV cache 오프로딩 백엔드를 **DAOS**로 구현하는 프로젝트.
LMCache의 `RemoteConnector` 인터페이스에 커넥터를 붙여, KV 청크를
DAOS DFS(`dfs_sys` API) 네임스페이스의 self-describing 파일로 저장한다.

## 아키텍처

```
vLLM ─ LMCache ─ RemoteBackend ─(plugin:// 스킴)─ DaosConnector (connector.py)
                                                   └─ DfsSys ctypes 바인딩 (dfs_binding.py)
                                                        └─ libdfs dfs_sys_* → DAOS Array 객체(파일)
```

- **키 매핑**: `CacheEngineKey` → `sha256` → DFS 경로 (flat, fanout은 Phase 4)
- **객체 레이아웃**: `serde.py` — `[prefix 8B][header][payload]`. 파일이 자기 자신을
  기술하므로 read 시 별도 stat/index 불필요.
- **동시성**: blocking libdfs 호출은 스레드풀 executor로 분리해 asyncio 루프 비블로킹.
- **열거·삭제**: `list()`는 `dfs_sys_opendir/readdir/closedir`, `remove_sync()`는
  `dfs_sys_remove_type`. `RemoteBackend.remove()`가 `remove_sync()`를 호출하므로
  이것이 원격 eviction의 유일한 경로다.

## 근거 (검증된 사실)

- DAOS **2.9.100** 소스: `daos/src/include/daos_fs_sys.h`
  (`dfs_sys_connect/_open/_read/_write/_close/_remove`), `daos_fs.h` (플래그).
- Samba **vfs_daos** 모듈이 동일하게 `dfs_sys` API 사용 → 호출 패턴 재사용.
- LMCache `RemoteConnector` 인터페이스: `async exists / exists_sync / async get /
  async put / async list / async close`, 키 타입 `CacheEngineKey`
  (`lmcache/v1/storage_backend/connector/base_connector.py`).

## 대상 버전 핀

**LMCache v0.5.2 + vLLM ≥ 0.26.0** 기준으로 구현·검증. LMCache v0.5.2 릴리스 노트가
"Requires LMCache v0.5.2 for vLLM ≥ 0.26.0"으로 명시한다. 런타임 박스에서
`pip show lmcache vllm`으로 확인 후 진행할 것. 버전이 다르면 `connector.py`의 API
배선과 아래 플러그인 URL 규칙을 재확인해야 한다.

## 플러그인 등록과 URL 규칙 (중요)

LMCache는 out-of-tree `RemoteConnector` 서브클래스를 `DynamicConnectorAdapter`로
자동 래핑한다. **실측한 계약은 다음과 같고, `daos://` 스킴은 동작하지 않는다.**

```python
# lmcache/v1/storage_backend/connector/__init__.py
schema = "plugin://%s" % extract_plugin_type(plugin_name)   # "plugin://daos"
def can_parse(self, url): return url.startswith(self.schema)  # startswith!
def create_connector(self, context):
    return self._connector_class(
        loop=..., local_cpu_backend=..., config=...)          # url 인자 없음
```

1. **스킴은 `plugin://`** — `daos://<pool>/<container>`는 어떤 어댑터에도 매칭되지
   않아 `CreateConnector`가 `No adapter found for URL: daos://...`로 실패한다.
   `can_parse`가 `startswith`이므로 pool/container를 path에 실어 보낼 수 있다:

   ```
   plugin://<plugin_name>/<pool>/<container>[?sys=<sysname>]
   ```

2. **생성자는 `url`을 받지 않고 `config`를 받는다** — 따라서 커넥터는 대상 pool/
   container를 `config.remote_url`에서 얻는다. `DaosConnector.__init__`은
   `(url=None, loop=None, local_cpu_backend=None, config=None)` 형태로, 어댑터 경유
   호출과 직접 생성(테스트) 양쪽을 모두 지원한다.

3. **플러그인명 형식은 `{type}` 또는 `{type}.{instance}`** — `extract_plugin_type`이
   `.` 앞부분만 스킴에 쓰므로 `daos.nvme` 같은 인스턴스 분리가 가능하다.

설정 예시는 `examples/lmcache_daos.yaml` 참고.

```yaml
chunk_size: 256
remote_url: "plugin://daos/hdd_pool/lmcache_test"
remote_serde: "naive"
remote_storage_plugins: ["daos"]
extra_config:
  remote_storage_plugin.daos.module_path: lmcache_daos.connector
  remote_storage_plugin.daos.class_name: DaosConnector
```

## 진행 상태

- [x] **Phase 0** 환경 조사 — 개발 박스엔 DAOS 런타임/LMCache 미설치. 실제 I/O는
      런타임 박스에서 (pool/POSIX 컨테이너 필요).
- [x] **Phase 1** libdfs ctypes 바인딩 + serde 프레이밍 + 단위테스트.
      **T1(DFS roundtrip) 실 DAOS 환경 PASS.**
- [x] **Phase 2 (코드)** 실제 LMCache v0.5.2 API로 커넥터 확정:
      `RemoteMetadata` 메타 코덱, `local_cpu_backend.allocate(...)` 재구성,
      플러그인 등록 방식.
- [x] **Phase 2 (검증)** **T2 커넥터 왕복 PASS** (LocalCPUBackend→put→exists→get,
      payload/shape/dtype 일치).
- [x] **Phase 2 잔여** 플러그인 스킴 라우팅 **실동작 확인 완료** — `daos://`가
      실제로 라우팅되지 않는 결함을 발견해 `plugin://` 규칙으로 수정.
      **T3(플러그인 라우팅) PASS.**
- [x] **Phase 3** vLLM+LMCache **miss→store→hit PASS** (아래 검증 결과 참고).
- [x] **Phase 3 정합성** 부분 저장 청크의 오탐 hit 방지 **PASS** — 잘린 객체에서
      예외가 서빙 경로로 탈출하던 결함을 발견해 miss 반환으로 수정 (T4).
      동일 키 동시 writer 경합 **PASS** (T5).
- [x] **Phase 3 멀티 replica** 독립 vLLM replica 2개가 공유 DAOS L2로 prefill 생략
      **PASS**, 동시 read 확장성 측정 완료 (T6).
- [ ] **Phase 3 잔여** **진짜 cross-node** 공유 — T6는 같은 호스트·같은 GPU의
      cross-replica라 네트워크 홉과 노드별 NIC/CPU 경합이 빠져 있다. 노드 2대 이상에
      replica를 배치해 재측정해야 한다.
- [x] **용량 관리 기반** `list()`(readdir) + `remove_sync()` 구현 **PASS** (T7).
      이전에는 `list()`가 `[]`이고 삭제 경로가 아예 없어 컨테이너가 단조 증가만 했다.
- [ ] **용량 정책** eviction/TTL/모델 revision 폐기 정책 설계. 32K prefix 하나가
      ~3.5 GiB, 64K는 ~7 GiB이므로 `nvme_pool`(7.5 GB)은 32K 2개면 찬다.
- [x] **batched 인터페이스** `batched_get`/`batched_put` 구현 **PASS** (T8).
      `batched_contains`는 측정 결과 이득이 없어 의도적으로 상속 유지 (아래 참고).
- [ ] **Phase 4** 벤치·튜닝(oclass EC/RP, chunk, in-flight), 디렉터리 fanout, RDMA.
- [ ] **Phase 5** eviction/용량관리, SRPM/CI 연계, 문서·HA.

## 검증 결과 (2026-07-30, ExaCI5-4 CI)

환경: 클라 192.168.35.40 (Rocky 8.10, NVIDIA A2 15356MiB, 드라이버 610.43.02,
CUDA 13.3, torch 2.11.0+cu130) / DAOS 2.9.100 서버 4 rank (.41 rank0·1, .42 rank2·3,
`ofi+verbs;ofi_rxm` on ib0 172.30.44.0/24) / vLLM 0.26.0 + LMCache 0.5.2 /
모델 Qwen3-1.7B.

### 기능 검증

| 테스트 | 내용 | 결과 |
|---|---|---|
| T0 | serde 프레이밍 (DAOS 불필요) | PASS |
| T1 | DFS roundtrip, 1 MiB write/read/remove | PASS |
| T2 | 커넥터 왕복 (직접 생성) | PASS |
| T3 | 플러그인 라우팅 (`CreateConnector` 경유) + `daos://` 거부 | PASS |
| T4 | 부분 저장(잘린) 객체 6개 절단 지점 → 오탐 hit 없이 miss | PASS |
| T5 | 동일 키 동시 writer 40라운드 × 6 writer, blend 검출 | PASS |
| T6 | 독립 replica 2개의 공유 L2 재사용 + 동시 read | PASS |
| T7 | `list()` 열거 + `remove_sync()` 삭제 (플러그인 래퍼 경유 포함) | PASS |
| T8 | `batched_get`/`batched_put` + 상속된 prefix 의미 | PASS |
| Phase 3 | vLLM miss→store(DAOS)→**프로세스 재시작**→hit(DAOS) | PASS |

Phase 3은 두 패스 사이에 vLLM을 완전히 재시작한다. GPU KV 캐시와 LMCache 로컬 CPU
계층이 모두 비워지므로, 2차 패스의 hit 출처는 DAOS뿐이다.

```
pass1: Stored 2048+2048+768 = 4864 tokens (0.5195 GB)
pass2: LMCache hit tokens: 4864, need to load: 4864     ← 전량 hit
       Retrieved 4864 out of 4864 required tokens. size: 0.5195 gb,
                cost 501.7759 ms, throughput: 1.0354 GB/s
       DAOS 파일 수·바이트 불변 → 재계산·재저장 없음
```

### 성능: 저장 계층이 손익분기를 가른다

풀만 바꾸고 나머지 조건을 고정한 실측. hit 토큰 수와 키 해시는 양쪽 동일.

| pool | 백킹 디바이스 | TTFT cold | TTFT warm | 결과 |
|---|---|---|---|---|
| `hdd_pool` | ZFS zvol (`/dev/zvol/daoshdd/daosdata`) | 2.035s | 2.147s | **0.95x — 손실** |
| `nvme_pool` | kdev NVMe | 1.986s | **0.706s** | **2.81x (TTFT 64.5%↓)** |
| `nvme_pool` (재현) | kdev NVMe | 1.781s | **0.678s** | **2.63x (TTFT 61.9%↓)** |

NVMe 결과는 독립 실행에서 2.63~2.81x로 재현된다.

NVMe 계층에서는 DAOS 로드 0.50s < prefill 재계산 1.99s로 손익분기 부등식이 성립한다.
zvol 계층에서는 로드가 prefill보다 느려 캐시가 오히려 손해다. **지연이 중요한 시험은
NVMe 백킹 풀을 쓸 것.**

측정 KV 크기(Qwen3-1.7B, 28층 / KV head 8 / head_dim 128, BF16):

```
토큰당 = 2(K+V) × 28 × 8 × 128 × 2B = 114,688 B = 112 KiB
  256 토큰 (chunk_size)  =  28.0 MiB   → DFS 파일 1개
 2048 토큰               = 0.2188 GiB  → LMCache가 8청크씩 묶어 store하는 단위
 4864 토큰 (본 시험)     = 0.5195 GiB  → DFS 파일 19개
```

dfuse 실측 557,843,220 B이 위 계산(557,842,432 B)과 파일당 36 B 프레이밍
(prefix 8 B + `RemoteMetadata` 28 B) × 19개를 더한 값과 일치한다. 계획서가 가정한
"32층 / 8 KV head / head_dim 128 / BF16 / 256-token → 약 32 MiB/chunk"와도
층수 비율(28/32)만큼 정확히 맞는다.

put throughput 4.8~9.8 GB/s (첫 put만 연결 초기화 비용으로 느림).

### 멀티 replica 공유 L2 (T6, `tests/phase3_multi_replica.sh`)

A2 한 장에 독립 vLLM replica 2개(`--gpu-memory-utilization 0.42` 각각)를 올리고 같은
DAOS 컨테이너를 공유시킨 결과.

| | TTFT | LMCache | DAOS retrieve |
|---|---|---|---|
| A: cold (miss→store) | 1.933s | `hit 0` | — |
| B: 공유 L2 hit (단독) | **0.703s** | `hit 4864, need to load 4864` | 501.8 ms / 1.0354 GB/s |
| A: 동시 | 0.795s | `hit 4864, need to load 4864` | 579.5 ms / 0.8965 GB/s |
| B: 동시 | 0.890s | `hit 4864, need to load 4864` | 642.5 ms / 0.8086 GB/s |

- replica B는 별개 프로세스로 자기 L1이 이 prefix를 본 적이 없고, 파일 수가 19개에서
  변하지 않았으므로 재저장이 아니라 DAOS 로드다. **cross-replica 2.75x.**
- 동시 read 시 TTFT는 단독 대비 +13% / +27%, 집계 대역폭은 1.035 → 1.705 GB/s로
  **1.65배 확장**. 동시 상황에서도 cold 대비 2.17~2.43x.
- 동시 구간은 두 replica를 **재시작한 뒤** 측정한다. 재시작 없이 재면 양쪽이 이미
  로컬 계층에 KV를 갖고 있어 `need to load: 0`이 되고, DAOS를 전혀 거치지 않는
  로컬 hit(TTFT 0.1s대)을 잘못 측정한다.

**범위 한정**: T6는 같은 호스트·같은 GPU이므로 cross-*replica*이지 cross-*node*가
아니다. 네트워크 홉도, 노드별 NIC/CPU 경합도 빠져 있다.

### batched 인터페이스 — 측정으로 범위를 좁힌 기록

LMCache v0.5.2는 `batched_get` / `batched_put` / `batched_contains`를
`support_*()` opt-in과 함께 제공한다. **`get`/`put`만 구현했다.** 근거는 실측이다.

19객체 배치(4864-token prefix 규모), `nvme_pool`:

| 청크 | PUT 순차 → batched | GET 순차 → batched |
|---|---|---|
| 1 MiB | 106.8 → 44.4 ms (**2.41x**) | 69.3 → 19.8 ms (**3.49x**) |
| 4 MiB | 248.1 → 152.8 ms (**1.62x**) | 133.9 → 54.9 ms (**2.44x**) |
| 16 MiB | 862.4 → 643.6 ms (**1.34x**) | 436.1 → 148.1 ms (**2.95x**) |

**`batched_contains`는 구현하지 않는다.** 가장 큰 이득처럼 보이는 자리다 — 상속
폴백이 `for key in keys: contains(key)` **순차 루프**이고 32K prefix면 128번
왕복이다. 그런데 존재하는 객체에 대한 `dfs_sys_open`+`close`가 개당 **약 69 µs**라
전체 순차 프로빙이 8.8 ms에 그치고, 윈도우 fan-out은 스레드 디스패치 오버헤드 때문에
9.4 ms(**0.9x**)로 오히려 느렸다. ~500 ms짜리 retrieve 옆에서는 어느 쪽이든 노이즈다.
`support_batched_contains()`는 False로 유지하며, T8이 이를 단정해 데이터 없이 되돌리는
것을 막는다. 재검토할 가치가 있는 경우는 hit rate가 낮은 대규모 운영에서 **지연이 아니라
낭비되는 서버 연산**을 문제 삼을 때다.

`batched_async_contains`와 `batched_get_non_blocking`도 상속 유지한다. 둘 다 이미
`asyncio.gather`로 우리 per-key 메서드를 fan-out하며, 특히 후자는 첫 실패 이후 객체에
`ref_count_down()`을 수행하는 수명 규약을 담고 있어 재구현 시 `MemoryObj` 누수 위험만
생긴다.

**진짜 batch RPC가 아니라는 점**을 분명히 해둔다. Redis는 `batch_exists_sync`로 1회
왕복이 가능하지만 DFS는 청크마다 별개 객체이므로 위 구현은 스레드풀 fan-out이다. N개
연산을 1회 요청으로 접으려면 계획서 §6 3단계의 dkey/akey 레이아웃이 필요하다.

**E2E 해석 주의**: batched 도입 후 Phase 3 E2E는 cold 2.009s → warm 0.665s(3.02x)로
나왔지만, 이전 실행이 2.63~2.81x였으므로 run-to-run 변동 범위 안이다. 서빙 읽기 경로는
상속된 `batched_get_non_blocking`이 **이미** gather로 병렬화하고 있었으므로, 이 변경의
E2E 효과는 "회귀 없음"으로 읽어야 하고 위 배수는 커넥터 API 수준 수치다.

주의: A2는 H100 대비 prefill 연산량이 훨씬 작으므로 위 배수를 H100 경제성 판단에
그대로 쓸 수 없다. 이 CI의 역할은 **기능·정합성 검증**이고 성능 게이트는 별도 H100
테스트베드에서 받아야 한다.

## 테스트

로컬(DAOS 불필요) — serde 로직:
```bash
python3 tests/test_serde.py
```

런타임 박스(DAOS 필요):
```bash
daos cont create <pool> <cont> --type POSIX

# T1: DFS roundtrip (LMCache 불필요)
DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> python3 tests/test_dfs_roundtrip.py

# T2: 커넥터 왕복 (LMCache 필요)
DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> python3 tests/test_connector_roundtrip.py

# T3: 플러그인 라우팅 (LMCache 필요)
DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> python3 tests/test_plugin_routing.py

# T4: 부분 저장(잘린) 객체가 오탐 hit이 되지 않는지
DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> python3 tests/test_partial_object.py

# T5: 동일 키 동시 writer (WRITERS/ROUNDS/PAYLOAD_KB로 조절)
DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> python3 tests/test_concurrent_writers.py

# T7: list() 열거 + remove_sync() 삭제
DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> python3 tests/test_list_and_remove.py

# T8: batched_get / batched_put + prefix 의미
DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> python3 tests/test_batched.py
```

GPU + vLLM이 있는 박스에서 (E2E):
```bash
# Phase 3: miss -> store -> 재시작 -> hit
POOL=<pool> CONT=<cont> bash tests/phase3_vllm_e2e.sh

# T6: replica 2개가 공유 L2 재사용 + 동시 read
POOL=<pool> CONT=<cont> bash tests/phase3_multi_replica.sh
```

T2~T5는 멱등하다 — 시작 시 대상 키를 제거하므로 반복 실행할 수 있다.

> `test_concurrent_writers.py` 주의: 커넥터는 생성 시 받은 asyncio 루프에 바인딩되고
> `그 루프.run_in_executor`로 blocking libdfs 호출을 넘긴다. 스레드마다 별도 루프를
> 만들어 구동하면 `got Future attached to a different loop`가 난다. LMCache는 항상
> 단일 루프를 쓰므로, 동시성은 루프 하나를 전용 스레드에서 돌리고
> `asyncio.run_coroutine_threadsafe`로 코루틴을 던지는 형태로 모델링해야 한다.

## 운영 주의사항 (실측 기반)

### PYTHONHASHSEED=0 필수

프로세스/노드 간 캐시 공유에는 **`PYTHONHASHSEED`를 고정해야 한다.** LMCache가 vLLM의
해시 함수를 못 불러오면 Python builtin `str` hash로 폴백하는데, 이 해시는 프로세스마다
salt가 달라 **같은 prompt가 다른 청크 키를 생성**한다. 결과적으로 재시작 후 hit이 0이
되고 캐시를 다시 저장한다.

증상과 증거:
```
WARNING: Centralized cache sharing detected but PYTHONHASHSEED not set.
pass1: Initialized NONE_HASH=14150773119372137151
pass2: Initialized NONE_HASH=17258592176669719754    ← 값이 다르면 절대 hit 안 됨
```
`export PYTHONHASHSEED=0`을 엔진 기동 **전에** 설정하고, cross-node 구성에서는 모든
노드에 동일 값을 강제할 것.

### 실환경에서 확인된 libdfs 규칙 (DAOS 2.8/2.9 빌드)

- `dfs_sys_connect` 전에 `daos_init()` **및 `dfs_init()`** 를 모두 호출해야 함
  (dfs_init 누락 시 EACCES=13). 바인딩에 반영됨.
- `dfs_sys_remove`는 이 빌드에서 **ENOTSUP(95)** → `dfs_sys_remove_type(path,
  force=False, mode=0, NULL)` 사용. `force=True`도 ENOTSUP 유발 → False 필수.
- 서버측: `daos_server` 유저가 kdev NVMe 블록디바이스(`root:disk 660`)를 열려면
  `usermod -aG disk daos_server` 필요 (안 그러면 bdev_aio_open Permission denied
  → 엔진 기동 실패).

### 클러스터/런타임 함정

- **신규 DAOS 풀 생성 실패(`DER_NOSPACE`)**: 크기와 무관하게 실패하면 SSD가 아니라
  엔진 ram-disk 고갈이다(`daos_server.yml`의 `class: ram, scm_size`). 서버 로그에
  `no SCM space available for metadata`가 남는다. MD-on-SSD 모드에서 신규 풀의 메모리
  파일이 ram-disk에 들어가야 하기 때문. → 여유 있는 기존 풀 재사용 또는 `scm_size`
  증설 후 엔진 재시작.
- **vLLM 프로세스 누수**: vLLM 워커는 프로세스명을 `VLLM::EngineCore`로 재작성하므로
  `pkill -f "vllm serve"`가 놓친다. 남은 워커가 GPU 메모리를 계속 점유해 다음 기동이
  `Free memory on device cuda:0 ... less than desired GPU memory utilization`으로
  죽는다. → `nvidia-smi --query-compute-apps=pid`로 GPU 점유 기준 회수할 것.
- **FlashInfer JIT**: 샘플링 커널을 nvcc로 런타임 컴파일하므로 드라이버만 설치된
  박스에서 `Could not find nvcc and default cuda_home='/usr/local/cuda' doesn't exist`
  로 실패한다. → `VLLM_USE_FLASHINFER_SAMPLER=0` 또는 `cuda-nvcc` 설치.
- **컨테이너 파일 확인**: `daos fs`에는 `ls` 서브커맨드가 없다. 파일 수/용량 확인은
  `dfuse --disable-caching` 마운트 후 `find`/`du`로.
- **Rocky 8(glibc 2.28)에서 vLLM**: vLLM 0.22.0+ 본체는 `manylinux_2_28` 휠이라
  호환되지만, 의존성 `llguidance`의 x86_64 휠은 `manylinux_2_31`뿐이다. pip이 sdist로
  폴백하고 maturin이 puccinialin으로 자체 Rust 툴체인을 받아 빌드하므로 통과한다
  (시스템 rust 불필요, 빌드 시간 소요). vLLM 0.26.0 직접 의존성 73개 중 비호환은
  이 하나뿐이었다.

## 알려진 미해결 지점

- **`list()`가 돌려주는 이름은 `CacheEngineKey`로 되돌릴 수 없다.** `_key_to_path`가
  sha256으로 해싱하므로 64자 다이제스트가 나온다. 용량 작업(개수·총 바이트·경로 단위
  일괄 삭제)에는 충분하지만, 이름에서 키를 복원하는 소비자에는 못 쓴다 — LMCache의
  `fs_connector`는 파일명에 키를 인코딩하고(`/` → `-SEP-`)
  `internal_api_server/vllm/load_fs_chunks_api`가 `CacheEngineKey.from_string`으로
  되돌린다. 되돌릴 수 있게 만들려면 온디스크 네이밍을 바꿔야 하고 기존 캐시가 전부
  무효화되며 디렉터리 fanout 설계와도 얽히므로, 조용히 바꾸지 않고 명시적 결정 사항으로
  남겨둔다.
- **용량 정책이 없다.** `list()`/`remove_sync()`로 수단은 갖췄지만 무엇을 언제 지울지는
  미정이다. 시험 중 컨테이너가 파일 2 → 59개(1.35 GB)로 단조 증가했다.
- `get()`이 반환한 `MemoryObj`의 수명 관리. 테스트 실행 시 LMCache가
  `MemoryObj at N is being garbage collected with ref_count=1, pin_count=0` 경고를
  낸다. 실제 서빙 경로에서는 LMCache가 관리하는 것으로 보이나, 장시간 구동 시 CPU
  메모리 풀 누수 여부는 별도 확인이 필요하다.
- **진짜 cross-node 공유는 미검증.** T6는 같은 호스트·같은 GPU의 cross-replica라
  네트워크 홉과 노드별 NIC/CPU 경합이 빠져 있다 (Phase 3 잔여).
- **메타데이터 스케일 미검증.** 본 시험은 파일 19개 규모여서 계획서 §3.3이 경고한
  "파일 수 수십만~수백만에서 metadata·디렉터리 분산이 먼저 병목" 구간에 닿지 않았다.
  `_key_to_path`는 여전히 flat(`/` + sha256)이라 fanout도 미적용이며, 이 스케일 시험
  결과가 dkey/akey native 레이아웃 투자의 근거가 된다.
