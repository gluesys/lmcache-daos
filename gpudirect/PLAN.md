# DaosGdsBackend 구현 계획 — 착수 판정: **부정 (보류)**

> **Phase B 측정 결과로 이 계획은 착수하지 않는다.** 마지막 근거였던 지연이 측정에서
> 반대로 나왔다: LMCache 의 실제 chunk 크기(Qwen3-14B 기준 40 MiB)와 실제 concurrency
> 에서 GDS 는 스테이징보다 **2.1배 느리고**, 6000 토큰 프롬프트 기준 TTFT 를 **+30.7 ms**
> 악화시킨다. 반면 복사를 완벽히 제거해서 벌 수 있는 것은 **2.8 ms** 다. 상세는
> `README.md` 의 "지연 이득 측정 (Phase B)".
>
> 아래 Phase 1~5 설계는 폐기하지 않고 남긴다. 전제(스토리지 티어가 GPU 당 17 GB/s 이상을
> 공급하고, QP 당 22 GB/s 제한을 우회할 방법이 생기는 것)가 바뀌면 그대로 유효하다.
> **재개 조건은 §0 끝에 적었다.** 2026-09-05 분리 드라이브 재측정(README 말미)에서도 gpu/staging 0.46~0.79 로 판정 불변.
>
> 대신 하기로 했던 두 가지도 끝났다:
> - **인터리브 적용: 완료·검증됨.** `run_vllm_daos.sh` 에 `numactl --interleave=all`.
>   vLLM 매핑 2478 줄 중 2381 줄이 `interleave`, 리트리브 중 소켓 분배 51/49.
> - **소형 전송 ~190 µs 조사: 종결.** GPU-direct 의 이득이 아니었다. 호스트 경로는
>   128 KiB 위에서, GPU 경로는 256 KiB 위에서 각각 튀며, 256 KiB 는 그 틈이다.
>   64·128 KiB 에서는 GPU 가 더 느리다. 진입점이 아니라 목적지 메모리 타입이 원인임을
>   `hostattr` arm(= `dfs_read_gpu` + 호스트 버퍼 = 486.5 µs, `pinned` 478.0 과 동일)으로
>   확정했다. 실제 chunk 크기에서는 호스트 경로가 더 빠르므로 운영 영향 없음.
>
> **그리고 더 중요한 것이 나왔다.** 운영 경로의 리트리브가 3.46 GB/s 로 raw DFS
> 스테이징(35 GB/s)의 **1/10** 이다. 리트리브는 prefill 전에 동기로 적재되어야 하므로
> 이 값은 DAOS 읽기를 포함한 진짜 end-to-end 값이다. 즉 **시간의 대부분이 DAOS 밖**
> (디시리얼라이즈·H2D·GIL·파이프라인)에 있고, 전송 계층을 바꿔서 얻을 몫은 그 일부다.
>
> (앞 판에서 store 의 `put_time` 0.13 ms 를 "DAOS 쓰기" 로 읽어 "700배" 라고 적었던 것은
> 철회한다. `batched_put()` 은 비동기 제출이라 그 타이머는 큐 입력 비용일 뿐이다. store
> 에 대해 말할 수 있는 것은 스토리지 쓰기가 동기 임계경로 밖에 있다는 것뿐이다.)
>
> **그 10배 격차를 분해했다** (`README.md` 의 해당 절). store·retrieve 모두 시간의
> **99% 이상이 GPU⇄CPU 복사**이고 토큰 처리는 0.8% / 0.13% 다. 원인은 LMCache 의
> `batched_to_gpu`/`batched_from_gpu` 가 이름만 배칭이고 실제로는 chunk 단위 Python
> 루프라는 것(LMCache 자신의 TODO). PCIe 순수 데이터 이동은 retrieve 시간의 **≤7.1%**
> 이므로, GDS 로 GPU 상주 버퍼를 만들어도 지배 항목인 chunk 당 고정비용은 남는다.
> **최우선 개선은 LMCache 배칭이고 스토리지와 무관하다** — Phase B 판정 유지.



목표는 vLLM/LMCache 의 KV-cache 경로에서 **호스트 스테이징 복사를 제거**하고 DAOS 의
`dfs_read_gpu()`/`dfs_write_gpu()` 로 GPU 메모리에 직접 읽고 쓰는 LMCache 스토리지
백엔드를 만드는 것이다. 주 사용처는 **GPU 8장 호스트**다.

전제 지식은 `README.md` 에 있다 — 여기서는 그 측정 결과를 설계 입력으로만 인용한다.

---

## 0. 착수 판정 (완료, 결론이 바뀌었다)

이 계획의 첫 항목은 "정당성을 재확인하는 측정" 이었다. 수행했고, **원래의 정당화 근거는
성립하지 않는 것으로 나왔다.** 그 결과를 숨기지 않고 계획의 전제로 바꿔 쓴다.

원래 논리는 "GPU 8장이면 스테이징의 DRAM 소비가 호스트 DRAM 을 포화시키므로 GDS 는
선택이 아니라 필요조건" 이었다. 그 논리의 분모(호스트 DRAM 상한)를 제대로 다시 재니:

| | 4판 (단순 루프) | 현재 (`tests/bench_dram_ceiling.c`) |
|---|---|---|
| COPY (1R+1W), 양 소켓 | 136.6 GB/s | **401.7 GB/s** |
| 소켓0 고정 | 미측정 | 220.6 GB/s |

분모가 2.9배 커졌으므로 결론이 바뀐다:

- **지금 이 스토리지 티어(DAOS 2노드, 집계 ~37 GB/s)에서는 GPU 8장이어도 스테이징으로
  충분하다.** 추론 126 + 스테이징 74 = 200 GB/s, 상한의 50%.
- 스테이징이 DRAM 벽에 닿는 지점은 **집계 retrieval 138 GB/s(GPU 당 17.3 GB/s)** 다.
  버퍼가 NIC 소켓에 몰리면 79 GB/s(GPU 당 9.9)로 내려간다.

**따라서 착수 조건은 "GPU 8장" 이 아니라 "스토리지 티어가 GPU 당 약 17 GB/s 이상을
공급하는 8장 구성" 이다.** 이 조건은 8장 배포에서 자연스럽다 — 그보다 적게 공급하면
KV 티어가 병목이 되어 GPU 가 굶기 때문이다. 지금 클러스터(2노드)는 그 조건을 만족하지
않으므로, **현 하드웨어에서 GDS 는 성능상 필요하지 않다.**

### 그래서 지금 무엇을 근거로 만드는가

DRAM 논거가 약해졌으므로 남는 근거를 명시한다. 강한 것부터:

1. **지연.** 스테이징은 전송이 끝난 뒤 H2D 복사를 직렬로 더 한다. TTFT 에 직접
   더해지는 몫이며 대역폭 여유와 무관하므로, 인터리브로도 없어지지 않는다. 남은 근거
   중 가장 강하다. **미측정** — 아래 Phase 5 의 1순위 항목이다.
2. **CPU.** 복사엔진 구동과 pinned 버퍼 관리가 추론의 CPU(토크나이즈·스케줄·샘플링)와
   경합한다. `cycles/byte` 는 쟀지만 8장에서 CPU 가 먼저 묶이는지는 미측정이다.
3. **스토리지 확장 헤드룸.** GPU 당 19.7 GB/s(집계 158)를 넘으면 스테이징이 벽에 닿는다.
   다만 GDS 자신도 QP 당 22 GB/s 에서 막히므로 이 근거로 벌 수 있는 폭은 좁다.

**약해진 근거를 강한 척 쓰지 않는다.** DRAM 절약(0.25 vs 1.75 B/B)은 실재하지만, 인터리브
적용 후 현 클러스터에서는 여유 자원을 더 여유롭게 만드는 것에 그친다. 이 프로젝트를
정당화하는 것은 지연이어야 하고, **그 지연 이득은 아직 측정되지 않았다.** 그러므로
Phase 1~4 에 착수하기 전에 지연을 먼저 재는 것이 옳다 — 아래 순서를 그렇게 바꿨다.

### NUMA 항목도 측정했고, 논거를 한 번 더 좁혔다

"버퍼 NUMA 배치만으로 해결되면 이 프로젝트보다 훨씬 싸다" 고 적었던 항목을 쟀다.
**그쪽이 맞았다.** 기본 상태에서 스테이징 DRAM 트래픽의 81% 가 소켓1 한쪽에 몰리고
(NIC·GPU 는 둘 다 소켓0), `numactl --interleave=all` 로 펴면 편중이 52% 로 내려가면서
대역폭은 오히려 34.77 → 35.46 GB/s 로 조금 오른다. 비용 0 의 개선이다.

교차점(측정된 편중 반영):

| 배치 | 집계 교차점 | GPU 당 |
|---|---|---|
| 기본 (S1 81% 편중) | 113 GB/s | 14.1 GB/s |
| **`--interleave=all`** | **158 GB/s** | **19.7 GB/s** |

GPU-direct 자신의 상한은 QP 당 22 GB/s 다. 따라서 **인터리브를 켠 뒤 GDS 가 대역폭으로
이기는 구간은 GPU 당 19.7 ~ 22 GB/s 라는 좁은 띠뿐이다.**

**즉 8-GPU 에서 먼저 해야 할 일은 이 백엔드를 만드는 것이 아니라 스테이징 버퍼를
인터리브로 배치하는 것이다.** 그것을 먼저 적용하고, 그 뒤에도 부족한지로 판단해야 한다.

남은 미검증 항목 하나: 8-GPU 추론의 DRAM 소비를 1-GPU 15.8 GB/s 의 선형 외삽으로 잡았다.
고정 오버헤드가 있으면 과대평가이고, 그러면 교차점은 더 올라간다 — GDS 에 더 불리한
방향이다. 8장 장비가 생기면 확인한다.

### Phase B 결과와 재개 조건

지연을 쟀고(`README.md` 의 해당 절), **반대 방향**이 나왔다:

| | 값 |
|---|---|
| 복사 제거로 벌 수 있는 최대 (chunk 지연 대비) | 2.5 ~ 15% |
| 실제 chunk(16 MiB)·실제 concurrency(16W)에서 GDS | 스테이징의 **2.09배 느림** |
| 6000 토큰 프롬프트(937 MiB) TTFT | 스테이징 28.0 ms → GDS 58.7 ms (**+30.7**) |
| 복사를 완벽히 제거했다면 | 25.2 ms (−2.8) |

GDS 가 이기는 구간은 chunk 256 KiB 뿐인데(0.36~0.59배), Qwen3-14B 의 토큰당 KV 가
160 KiB 이므로 그것은 1.6 토큰이다 — 설정으로 도달할 수 없다.

**재개 조건** (하나라도 성립하면 위 Phase 1~5 를 다시 검토한다):

1. **QP 당 22 GB/s 제한을 우회할 방법이 생긴다.** 이것이 GDS 열세의 유일한 원인이다.
   포트 하나에서 QP 를 늘리는 DAOS/Mercury/UCX 설정, 또는 NIC 포트 추가(멀티레일
   `UCX_MAX_RNDV_RAILS` 가 실제로 동작하는 구성).
2. **토큰당 KV 가 훨씬 작은 모델이 주 대상이 된다.** chunk 가 1 MiB 이하로 내려가면
   측정에서 GDS 가 동등~우세로 바뀐다. 단 그 전에 아래 3번을 먼저 시도해야 한다.
3. **소형 전송 호스트 경로의 ~190 µs 를 못 없앤다.** 256 KiB 에서 GDS 우세의 원인은
   복사 제거가 아니라 호스트 경로의 chunk 당 ~190 µs 추가 오버헤드다. 그것이
   `DaosConnector` 에서 고쳐지면 2번 조건도 사라진다. **먼저 이쪽을 조사하는 것이 싸다.**

---

## 1. 설계를 규정하는 측정 사실

| 사실 | 설계에 미치는 영향 |
|---|---|
| GPU 당 수요 = 집계/8. 교차점에서도 17.3 GB/s ≪ 22 (1 QP BAR 천장) | **per-QP 핸디캡이 구속하지 않는다.** 단일 GPU 에서의 열세가 8장에서는 사라진다 |
| 워커 프로세스마다 CaRT 컨텍스트 → 자체 QP | 8 프로세스 = QP 8개 이상 → 22 GB/s 천장을 구조적으로 우회. 상한 176 GB/s |
| store 방향은 페널티 없음 (34.57 GPU vs 35.12 host) | **store 를 먼저 적용**하는 비대칭 구성이 가능 |
| `RP_2G4` + chunk 4 MiB 가 최적, `RP_2G8` 은 역효과(20.0→12.7), EC 는 draft 가 거부 | 컨테이너 파라미터를 시작 시 **검증하고 거부**. 이 두 값이 결론을 세 번 뒤집었다 |
| LMCache async loading + GPU 백엔드 = hang | **동기 + 스레드풀 고정**, `enable_async_loading: False` |
| `daos_server` 재시작이 SPDK wedge → 풀 파기 | **서버 설정 변경을 요구하는 설계 금지** |
| DRAM 배수 gpu 0.10~0.30 / pinnedcopy 1.93~2.02 / hostcopy 4.18~4.73 | 합격 기준은 대역폭이 아니라 **DRAM 배수**로 잡는다 |

---

## 2. Phase 1 — v2 저장 포맷

LMCache·DAOS 없이 진행 가능하고 단위 테스트만으로 검증된다. Phase 2 와 병행.

현재 포맷(`lmcache_daos/serde.py`)은 `[prefix '<II' meta_len,payload_len = 8B][meta][payload]`
로, payload 가 비정렬 오프셋에서 시작해 GPU 등록·DMA 에 부적합하다.

```
offset 0     4 KiB 헤더 페이지
             magic, version, state, header CRC, meta_len, payload_len,
             RemoteMetadata
offset 4096  payload  (GPU 로 직접 DMA)
```

- 쓰기 순서: temp 객체 → payload → `COMMITTED` 헤더 → `dfs_move()` atomic publish.
  `tests/test_torn_object.py` 가 v1 에서 확인한 찢김 시나리오를 v2 에서 재사용한다.
- v1 과 **네임스페이스 분리**(컨테이너는 공유). 두 경로가 공존해야 한다 — Phase 4 의
  비대칭 구성(store=GDS, retrieve=CPU)이 같은 객체를 읽고 쓴다.
- key→path 는 현 커넥터의 `sha256 hexdigest` 평면 규칙을 승계한다. LMCache `GdsBackend`
  의 `str(chunk_hash)[:2]/[2:4]` 규칙은 **물려받지 않는다** — 부호 있는 int 라 `-1` 같은
  디렉터리 이름이 생긴다.
- 산출: `lmcache_daos/serde_v2.py`, `tests/test_serde_v2.py`

## 3. Phase 2 — native shim

Python 이 raw CUDA 포인터를 소유하면 안 되므로 C 계층이 필요하다. 기존
`shim/daos_evshim.c` 와 같은 자리에 둔다. GPU 8장이므로 `device_id` 가 1급 인자다.

```c
lcdg_context_open(pool, cont, sys, &ctx);
lcdg_file_open(ctx, path, flags, &file);
lcdg_buf_register(ctx, cuda_ptr, len, device_id, &reg);   /* 긴 수명 slab */
lcdg_read (file, reg, dst_off, len, file_off, &done);      /* dfs_read_gpu  */
lcdg_write(file, reg, src_off, len, file_off, &done);      /* dfs_write_gpu */
lcdg_wait(done);
```

- `daos_mem_attr_t{ma_mem_type = DAOS_MEM_TYPE_CUDA, ma_device_id = <ordinal>}`
- slab 은 **한 번 등록해 재사용**. 32 워커까지 붕괴가 없었던 것은 버퍼를 재사용했기
  때문이다.
- `lcdg_wait` 의 의미를 "submit 완료" 가 아니라 **"GPU 에서 안전히 소비 가능"** 으로
  문서화하고 테스트한다. 이 정의가 흐려지면 조용한 데이터 손상이 된다.
- `dfs_write_gpu` 는 **DAOS 수준에서 한 번도 테스트되지 않았다**(store 측정은 전송
  계층이었다). 이 Phase 의 첫 산출물은 write 왕복 게이트여야 한다.
- 산출: `shim/lmcache_daos_gds.c`, `tests/dfs_gpu_rt.c` 를 shim 경유로 확장

## 4. Phase 3 — `DaosGdsBackend`

기존 커넥터의 최대 강점(**LMCache 상류 미수정**)을 유지한다. `remote_storage_plugins` 와
같은 방식으로 `storage_plugins` 에 out-of-tree 모듈을 붙인다.

```yaml
storage_plugins: ["daosgds"]
extra_config:
  storage_plugin.daosgds.module_path: lmcache_daos.gds_backend
  storage_plugin.daosgds.class_name: DaosGdsBackend
  daosgds.pool: gdspool
  daosgds.container: kvgds_v2
  daosgds.mode: preferred        # required | preferred | disabled
  daosgds.io_workers: 16
enable_async_loading: False
```

- **T-check 를 이 Phase 의 첫 작업으로 둔다**: `storage_plugins` 가 out-of-tree
  `module_path` 를 받아주는지. `arm.yaml` 의 `DaxBackend` 는 in-tree 경로였다. 안 되면
  이 Phase 의 형태 자체가 바뀐다(상류 포크가 필요해진다). 확인 전에 나머지를 쓰지 않는다.
- meta 는 호스트로, payload 만 GPU 로 — **2단 read**
- 계약: `get_into(key, device_span)`, `put_from(key, device_span, meta)`
- `mode: required` 는 compat 폴백 시 실패, `preferred` 는 CPU 커넥터로 폴백
- 시작 시 컨테이너 검증: oclass 가 replicated 인지, chunk 가 4 MiB(`4194304`, 십진
  `4M` 아님)인지. 아니면 **거부**한다.
- 산출: `lmcache_daos/gds_backend.py`

## 5. Phase 4 — store 우선 적용

retrieve 보다 **store 를 먼저** 넣는다.

- store 는 전송 계층에서 페널티가 없다 (34.57 vs 35.12)
- store 는 **decode 와 겹쳐** 돌기 때문에 그 시점의 DRAM·CPU 절약이 TTFT 에 기여한다
- retrieve 는 8-GPU 실측으로 per-GPU 수요가 22 GB/s 아래임을 확인한 뒤 켠다

초기 배포는 `store=GDS, retrieve=CPU 커넥터` 비대칭 구성이고, 두 경로가 같은 v2
네임스페이스를 공유한다. `DaosConnector` 는 기본값으로 남는다.

## 6. Phase 5 — 8-GPU 통합 측정

| 기준 | 목표 | 방법 |
|---|---|---|
| 호스트 DRAM 배수 | ≤ 0.3 B/B | `uncore_imc/cas_count_*`, 유휴 기준선 차감 |
| **TTFT p50/p95** | 스테이징 대비 개선 | vLLM 수준. **DRAM 논거가 약해진 지금 이것이 1순위 지표다** |
| 집계 retrieval | 스테이징 대비 ≥ 1.0× | 8 워커 동시, 5회 중앙값 |
| direct 비율 | `required` 에서 compat 0건 | 백엔드 카운터 `direct`/`host_fallback` |
| concurrency | 8 워커까지 붕괴 없음 | 워커 sweep |

DRAM 배수는 이미 예상되는 결과(0.23)이므로 **판정 지표가 아니라 회귀 감시 지표**로
격하한다. 판정은 TTFT 와 집계 처리량이 한다.

---

## 7. 순서와 일정

Phase 0 의 결과 때문에 원래 순서를 바꿨다. **지연 이득을 먼저 재고, 그 결과로 착수를
결정한다.**

```
Phase A  인터리브 적용 (비용 0, 즉시)          -- 스테이징 쪽을 먼저 최적화
   |
Phase B  지연 이득 측정 (1-2일, 현 장비 가능)  -- ★ 착수 판정
   |     chunk 당 H2D 직렬 복사가 TTFT 에 얼마를 더하는가.
   |     이미 있는 5 arm 벤치의 chunk_ms 차이(gpu 29.96 vs pinnedcopy 15.44)는
   |     대역폭 열세에 섞여 있어 그대로 쓸 수 없다. 같은 대역폭으로 맞춘 뒤
   |     복사 몫만 분리해야 한다.
   |
   +---- 이득이 유의하지 않다면: 여기서 멈추고 DaosConnector + 인터리브로 간다.
   |
   +-- Phase 1 v2 포맷      (3-5일, 독립)  --+
   +-- Phase 2 shim + write 게이트 (3-5일) --+
                                             |
                              Phase 3 백엔드 (T-check 후 1주)
                                             |
                              Phase 4 store 적용 (3일)
                                             |
                              Phase 5 8-GPU 측정 (3일)
```

Phase 1·2 는 서로 독립이므로 병행한다. Phase B 를 건너뛰고 구현에 들어가면, 2~3주를
쓴 뒤에 "이득이 없다" 를 알게 될 위험이 있다 — 지금까지 네 번의 정정이 모두 그런
종류였다.

## 8. 알려진 함정

이미 대가를 치른 것들이다. 상세는 `README.md` 의 "알려진 한계".

- **서버 설정을 바꾸는 설계를 하지 말 것.** `daos_server` 재시작마다 SPDK wedge →
  전체 wipe + format → 풀 파기다.
- **`scons --build-deps=yes` 재실행이 UCX·Mercury 패치를 조용히 삭제한다.** 빌드는
  성공하고 런타임에만 `ucp_mem_map()` `-EINVAL` 로 돌아간다. `apply-patches.sh deps`
  를 다시 돌리고 두 컴포넌트를 재빌드해야 한다.
- 클라이언트 전제: nvidia **open** 커널 모듈, `libcudart.so.13`, `libgdrapi.so.2`,
  agent `domain: mlx5_0:1`. 하나만 빠져도 실패하고 증상이 서로 다르다. 모듈 교체 후
  `depmod -a` 를 빼면 closed 모듈이 계속 로드된다.
- LMCache 0.5.2 에 pin. `storage_plugins` 인터페이스는 상류 변경에 취약하므로 어댑터를
  얇게 유지한다.
- 측정 시 **분모도 분자와 같은 엄격함으로** 잴 것. 4판의 결론을 뒤집은 것은 새 분자가
  아니라 제대로 잰 분모였다.
