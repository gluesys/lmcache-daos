<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# rank 가드 — 축출(용량 관리) 재설계

상태: 1~4 단계 끝(2026-10-26). 1~3 단계 구현·레인 A 확인(2026-10-24, §10), 4 단계는 MP 어댑터의 승인
제어·punch 보류(1~3 단계에 같이 들어감)와 daos-operator 차트 값(`rankGuard.*`, 차트 0.1.9)이고 차트 경로로
다시 쟀다(§10.1). MP 희생자 순서는 §4.6 대로 LMCache 정책을 따른다. 현재 축출은 `rank_guard.CapacityIndex` + `connector._evict_loop`
(in-process)와 LMCache L2 축출(MP)이다.

## 1. 왜 다시 설계하나

DAOS 에는 축출이 없다. 캐시가 풀을 채우지 않게 하는 일은 전부 클라이언트 몫이다. 지금 방식은
"프로세스가 자기가 쓴 키의 논리 바이트를 세다가, 상한을 넘으면 가장 오래된 키를 punch" 한다.
레인 A 실측에서 이 방식이 맞지 않는 곳이 다섯 군데 보였다.

| # | 관찰 | 출처 |
|---|---|---|
| 1 | 물리 사용이 논리 상한의 **2.5~3 배**. punch 한 공간은 aggregation 이 돌아야 돌아오고, 64 버킷이 target 에 고르게 퍼지지 않는다 | RANK-GUARD.md §4 |
| 2 | `self_heal:none` 풀은 rank 하나가 내려가 있는 동안 **살아 있는 모든 rank 의 aggregation·GC 가 멈춘다**. punch 는 공간을 돌려주지 않고 오히려 tombstone 을 쓴다 | RANK-GUARD.md §4 |
| 3 | 4 분 장애에 3 GiB 상한(13 GB 풀)에서 DER_NOSPACE 270 회. 20 분 장애에서 재배치 켬은 정지 4.5 분 뒤부터 NOSPACE 889 회(끔 1 회). 재배치 예산(0.75 GiB)을 두면 374 회 — 예산이 2 분에 소진된 **뒤** 정지 5 분째부터 나므로 남은 몫은 살아 있는 rank 자기 쓰기와 축출 punch 다 | RANK-GUARD.md §4, RANK-GUARD-RELOCATION.md §6.1 |
| 4 | NOSPACE 는 풀 전체가 아니라 **target 하나**가 찰 때 난다. 풀의 남은 공간은 충분해도 한 target 이 차면 그 target 의 버킷 쓰기가 실패한다 | 10-21·10-24 로그(특정 rank 의 특정 tag) |
| 5 | 상한이 프로세스마다 따로다. 한 컨테이너를 여러 vLLM 이 쓰면 합이 묶이지 않고, 시작 스캔은 남의 키까지 "가장 오래된 내 키"로 넣는다 | `CapacityIndex` docstring |

부하 쪽 사실 하나 더: KV 캐시 쓰기의 상당 부분은 **한 번 쓰고 다시 읽지 않는 키**다(처음 보는
프롬프트, 대화의 마지막 청크). run K 부하에서 50 번째마다 오는 처음 보는 프롬프트가 쓰기량의
대부분을 차지했다. 순수 LRU 는 이런 키가 들어올 때마다 오래 쓰인 공통 접두(시스템 프롬프트 등)를
밀어낸다.

## 2. 목표와 비목표

목표
1. **target 단위 물리 공간**을 기준으로 상한을 지킨다. 논리 바이트 상한은 보조로만 쓴다.
2. NOSPACE 를 맞기 **전에** 쓰기를 거른다(승인 제어). 거르는 순서는 가치가 낮은 쓰기부터:
   재배치 쓰기 → 처음 보는 키 → 이미 여러 번 쓰인 키의 갱신.
3. 장애 중(살아 있는 rank 의 회수가 멈춘 동안)에는 punch 를 멈추고, 복귀 뒤 밀린 축출을 한다.
4. 한 번 쓰고 끝나는 키가 자주 쓰이는 키를 밀어내지 않게 한다.
5. 여러 프로세스가 한 컨테이너를 써도 조정 없이 전체 압력에 반응한다.

비목표
- 키 단위 정확한 전역 LRU(DAOS 에 공유 색인을 두는 것). 비용이 이득보다 크다(§6).
- DAOS 쪽 변경. 클라이언트만 바꾼다.
- EC 풀의 rebuild 공간 관리. EC 는 rebuild 가 공간을 쓰므로 별도 규칙이 필요하다(§8).

## 3. 지금 구조

```
put 성공 ─▶ CapacityIndex.add(key, 바이트)            (프로세스 메모리, OrderedDict)
hit     ─▶ CapacityIndex.touch(key)
1 초마다 ─▶ total > capacity 이면 오래된 키부터 punch, low(0.9×capacity) 아래까지
            breaker 가 닫히지 않은 rank 의 키는 건너뜀(남겨 두고 다음에)
시작    ─▶ 버킷마다 list_dkeys + 메타 akey 로 길이를 읽어 add_old(가장 오래된 쪽에)
MP      ─▶ LMCache L2 가 max_capacity_gb 로 축출하고 delete 를 부름
```

## 4. 제안

### 4.1 공간 신호: target 별 남은 공간

- `daos_pool_query_target(poh, tgt, rank, &info, ev)` 로 target 마다 `ta_space.s_free/s_total`
  을 읽는다(DAOS 2.8 `daos_pool.h`). 이벤트 기반이라 지금 DeadlineEngine 으로 기한을 건다.
- 대상은 **버킷이 있는 target 만**(버킷을 열 때 layout 으로 rank·target 을 이미 안다, `_first_rank`
  확장). 64 버킷이면 많아야 12 target 남짓.
- 주기 10 s(설정 가능). breaker 가 열린 rank 의 target 은 묻지 않는다.
- 값은 "target 의 남은 비율 f(t) = s_free / s_total" 하나로 줄인다. MD-on-SSD 는 데이터(NVMe)와
  메타(메모리 파일) 두 매체가 있으므로 **둘 중 작은 비율**을 쓴다.
- 신호가 없을 때(조회 실패·시작 직후)는 지금처럼 논리 바이트 상한만으로 동작한다.

### 4.2 수위와 행동

target 마다 세 수위를 둔다. 기본값은 풀의 크기와 무관한 비율이다.

| 수위 | 기본 | 넘으면(남은 비율이 이 아래면) |
|---|---|---|
| `evict_at` | 30 % | 그 target 의 버킷에 있는 키부터 축출 시작(§4.3), `stop_at` 위로 돌아올 때까지 |
| `admit_new_at` | 15 % | 그 target 으로 가는 **재배치 쓰기와 처음 보는 키(소형 대기열 진입)** 를 거름 |
| `admit_all_at` | 5 % | 그 target 으로 가는 쓰기를 모두 거름(NOSPACE 직전 정지) |

- 거른 쓰기는 지금의 "breaker 열림" 과 같이 처리한다: 저장하지 않고 캐시 miss 로 끝난다.
- 재배치 쓰기 예산(feat/relocate-space-brake)은 이 표의 `admit_new_at` 에 흡수된다. 신호가 없을
  때의 안전장치로만 남긴다.

### 4.3 희생자 고르기: S3-FIFO 변형 + target 우선

키 단위 순서는 **S3-FIFO**(작은 FIFO S 10 % + 주 FIFO M 90 % + 유령 G)를 쓴다.

- 새 키는 S 에 들어간다. S 에서 나갈 때 그동안 hit 이 한 번 이상이면 M 으로 옮기고, 없으면 punch
  하고 키 해시만 G 에 남긴다.
- G 에 있는 키가 다시 쓰이면(한 번 밀려났다가 또 필요해진 키) 곧바로 M 에 넣는다.
- M 은 hit 비트(최대 3)를 가진 FIFO. 꼬리에서 비트가 있으면 줄이고 다시 머리로, 없으면 punch.

이유: 한 번 쓰고 끝나는 키(처음 보는 프롬프트)는 S 에서 금방 빠지고, 여러 번 읽힌 공통 접두는
M 에 남는다. LRU 와 달리 hit 마다 자료구조를 옮기지 않아(비트만 세움) hit 경로가 싸다.

target 우선: 축출이 필요한 target 이 정해지면(§4.2) **그 target 의 버킷에 있는 키만** 대상으로
위 순서를 적용한다. 큐는 버킷별로 나눠 두고(64 개), target 단위 압력이 있을 때 그 target 의
버킷 큐들에서만 뽑는다. 압력이 없는데 논리 상한만 넘었을 때는 전체에서 뽑는다.

### 4.4 장애 중

breaker 가 열린 rank 가 하나라도 있으면(= `self_heal:none` 풀에서 살아 있는 rank 의 회수가 멈춘 상태):

1. **punch 를 멈춘다.** 희생자는 고르기만 하고 "보류 목록"에 쌓는다. punch 는 공간을 돌려주지 않고
   tombstone 만 쓴다.
2. 승인 제어(§4.2)를 그대로 한다. target 신호가 공간이 줄어드는 속도까지 알려 주므로, 남은 비율이
   `admit_new_at` 에 닿으면 재배치·새 키부터 거른다. 20 분 장애 실측의 "정지 4.5 분 뒤부터 NOSPACE"
   대신 "정지 몇 분 뒤부터 새 키 저장 중단, 기존 키 hit 유지"가 된다.
3. 복귀(모든 breaker 닫힘) 뒤 보류 목록을 punch 하고, 공간 신호가 회복될 때까지(aggregation 이 밀린
   회수를 끝낼 때까지) `evict_at` 판단을 30 s 늦춘다(10-21 실측: 복귀 직후 10 s 에 회수가 몰림).

EC 풀(`EC_2P1G1`, 기본 self_heal)은 rank 가 빠지면 exclude·rebuild 가 돌고 회수가 멈추지 않는다.
1 은 적용하지 않고 2·3 만 쓴다.

### 4.5 여러 프로세스

공유 색인을 두지 않는다. 각 프로세스는 **자기가 쓴 키만** 자기 큐에 두고, 공간 신호는 모두가 같은
target 값을 본다. target 하나가 `evict_at` 아래로 가면 그 target 에 키를 둔 프로세스가 모두 자기
꼬리부터 축출하므로, 전체 압력에 맞춰 함께 줄어든다(각자 쓴 만큼 비례해서).

- 시작 스캔에서 찾은 키(남이 썼을 수도 있음)는 **유령 G 와 별도의 "주인 없음" 큐**에 넣고, 압력이
  있을 때 맨 먼저 축출한다. 지금의 add_old 와 같은 효과지만 자기 키의 순서를 흐리지 않는다.
- 논리 바이트 상한(`DAOS_RANK_GUARD_CAPACITY_GB`)은 프로세스당 보조 상한으로 남긴다.

### 4.6 MP 모드

LMCache L2 축출이 희생자를 고르고 delete 를 부른다. 바꿀 수 있는 것은 두 가지다.

- 승인 제어(§4.2)를 L2 어댑터의 store 경로에 넣는다: 거른 store 는 실패로 돌려준다(LMCache 는 L2
  store 실패를 캐시 미적재로 처리한다).
- 장애 중 delete(= punch) 보류(§4.4 1)를 어댑터에서 한다: 보류 목록에 쌓고 복귀 뒤 실행.

희생자 순서(S3-FIFO)는 LMCache 의 정책을 따른다(어댑터가 바꾸지 않음).

### 4.7 시작 스캔

지금처럼 버킷마다 list_dkeys + 메타 길이를 읽되, 키를 "주인 없음" 큐로 넣는다(§4.5). breaker 가
열린 rank 의 버킷은 건너뛰고, 그 rank 가 닫히면 그 버킷만 다시 스캔한다(지금은 시작 때 한 번).

## 5. 설정

| 변수(in-process) / MP 키 | 기본 | 뜻 |
|---|---|---|
| `DAOS_RANK_GUARD_SPACE_POLL` / `guard_space_poll` | 10 s | target 공간 조회 주기, 0 이면 끔(지금 동작) |
| `DAOS_RANK_GUARD_EVICT_AT` / `guard_evict_at` | 0.30 | §4.2 |
| `DAOS_RANK_GUARD_ADMIT_NEW_AT` / `guard_admit_new_at` | 0.15 | §4.2 |
| `DAOS_RANK_GUARD_ADMIT_ALL_AT` / `guard_admit_all_at` | 0.05 | §4.2 |
| `DAOS_RANK_GUARD_CAPACITY_GB` / `max_capacity_gb` | 그대로 | 프로세스당 논리 상한(보조) |

차트(`rankGuard`)에는 `spacePoll`, `evictAt`, `admitNewAt`, `admitAllAt` 로 노출한다.

## 6. 고려했다 버린 방법

| 방법 | 버린 이유 |
|---|---|
| DAOS 에 공유 색인(키별 접근 시각을 KV 객체에) | 모든 hit 이 쓰기가 되어 쓰기량이 늘고, 그 객체가 있는 rank 가 죽으면 축출 전체가 멈춘다 |
| 풀 질의(`daos_pool_query`)의 `ps_free_min` | 한 번에 모든 target 을 묻는다. 장애 중에는 답하지 않는다(2026-10-22, self_heal none 풀 `pool query` 무응답) |
| 순수 LRU 유지 + 상한만 낮춤 | 한 번 쓰는 키가 공통 접두를 밀어내는 문제가 그대로다. 물리 2.5~3 배 편차도 그대로다 |
| 장애 중에도 punch 계속 | 회수가 멈춰 공간이 늘지 않고 tombstone 만 쓴다(RANK-GUARD.md §4) |
| 버킷 수를 크게(예: 256)만 늘려 target 편차를 줄임 | 편차는 줄지만 상한·장애 문제는 남는다. §4 와 함께라면 고려할 만하다(§8) |

## 7. 시험 계획

1. 단위 시험(`tests/test_rank_guard.py`, 가짜 공간 신호)
   - S3-FIFO: 한 번 쓰고 끝나는 키가 여러 번 읽힌 키보다 먼저 나간다. 유령 재진입이 M 으로 간다.
   - target 압력: 축출이 그 target 의 버킷 키에서만 나온다.
   - 수위: 재배치·새 키가 `admit_new_at` 에서, 전부가 `admit_all_at` 에서 걸러진다.
   - 장애 중 punch 보류, 복귀 뒤 실행.
   - 시작 스캔 키가 "주인 없음" 큐로 가서 먼저 나간다.
2. 레인 A(run K 부하, 13 GB 풀)
   - 정상 2 시간: 물리 사용(target 별 최소 남은 비율)이 `evict_at` 근처에 머무는지, hit 률이 LRU 와
     같거나 나은지.
   - 처음 보는 프롬프트 비율을 50 번째마다 → 5 번째마다로 늘린 부하: 반복 프롬프트 hit 유지(LRU 대비).
   - 20 분 장애(재배치 켬): NOSPACE 0~수 건, 장애 중 반복 hit 유지, 복귀 뒤 보류 punch 와 공간 회복.
   - vLLM 두 개가 한 컨테이너: 두 프로세스의 쓰기 비율대로 축출되는지.
3. MP 모드로 장애 시험 한 번.

## 8. 열린 질문(구현 전에 확인)

1. **(해결, 2026-10-24)** `daos_pool_query_target` 이 장애 중 살아 있는 target 에 대해 답하는가 — 답한다
   (20 분 장애 내내 값 갱신). 이하 당시 기록. 소스(2.8 `src/pool/cli.c`
   `dc_pool_query_target`)로는 `POOL_QUERY_INFO` 를 풀 서비스 리더에 보내고(`rsvc_client_choose`) 리더가
   그 rank 에 묻는다. 서비스 정족수와 그 rank 가 살아 있으면 답할 것으로 보이나 레인에서 확인한다.
   답하지 않으면 장애 중 신호는 "장애 직전 값 − 그 뒤 쓴 바이트 추정"으로 한다.
2. MD-on-SSD 에서 메타(메모리 파일) 비율이 데이터보다 먼저 차는 경우가 있는가. 10-21 tier-ratio 계산
   (target 당 메타 128 MiB 이상)과 함께 본다.
3. punch 직후 `s_free` 가 언제 오르는가(aggregation 주기). `evict_at` 판단의 이력 현상(hysteresis) 폭을
   정한다.
4. 버킷 수 64 → 256 의 효과(target 편차)와 시작 스캔 비용.
5. EC 풀의 rebuild 공간: rebuild 가 도는 동안 승인 수위를 올릴지.

## 9. 구현 순서

1. 공간 신호(target 조회, 기한, 버킷 → target 매핑) + 로그만. 레인에서 신호 품질 확인(열린 질문 1·3).
2. 승인 제어(§4.2) + 장애 중 punch 보류(§4.4). 재배치 예산을 대체.
3. S3-FIFO 큐와 target 우선 축출(§4.3), 시작 스캔 변경(§4.7).
4. MP 어댑터(§4.6), 차트 값.

각 단계가 따로 머지될 수 있게 나눈다. 1·2 만으로도 20 분 장애의 NOSPACE 문제는 풀린다.

## 10. 실측 (레인 A, 2026-10-24)

조건: RANK-GUARD-RELOCATION.md §6.1 과 같다(13 GB 풀, 용량 3 GB, run K 부하, rank 1 1200 s 정지, 재배치 켬).

| | 예산 없음 | 재배치 예산 | 1~3 단계(최종) |
|---|---|---|---|
| DER_NOSPACE | 889 | 374 | **3**(복귀 1 분 뒤, rank 1 target 값이 다시 들어오기 전) |
| 장애 중 반복 p50 | 0.18 s | 0.17 s | 0.17 s |
| 정상 반복 p50 | 0.163 s | 0.157 s | 0.157 s |
| 요청 / 실패 | 10,866 / 0 | 10,943 / 0 | 11,015 / 0 |
| 승인 제어로 거른 쓰기 | — | — | 275 |

- 가장 적게 남은 target 이 정지 7 분에 4 % 아래로 내려가고, 그 뒤 `ADMIT_ALL_AT` 이 쓰기를 미리 거른다.
- 복귀 뒤 공간 기준 축출은 조회 주기에 한 번(그러지 않으면 회수 지연 동안의 낮은 값으로 2.5 → 0.6 GiB 과잉 축출).
- 공간 조회를 이벤트·별도 poller 로 하면 정상 hit TTFT 가 0.158 → 0.181 s 로 늘었다. 동기 조회(전용 스레드)로 해소.

원자료: `daos-reboot-repro/2026-10-24-longout/`(`lo1`~`lo5`, `ab*`, `NOTES.md`).

### 10.1 차트 경로 (레인 A, 2026-10-26)

daos-operator 차트 `vllm.services` 로 띄운 vLLM(이미지 `vllm-lmcache-daos:0.30.0-0.5.5-20261025.2`, 기본값,
`lmcacheCpuGB: 0.5`)과 같은 조건(14 GiB 풀, rank 1 1200 s 정지).

| 요청 / 실패 | 반복 p50 정상 / 장애 중 / 복귀 뒤 | DER_NOSPACE | 승인 제어로 거른 쓰기 | 축출 뒤 크기 |
|---|---|---|---|---|
| 15,662 / 0 | 0.091 / 0.094 / 0.094 s | 5(복귀 1 분 뒤) | 933 | 2.69 GiB |

라이브러리 기본값만으로 §10 의 결과가 재현된다. 원자료: `daos-reboot-repro/2026-10-26-chart/`.
