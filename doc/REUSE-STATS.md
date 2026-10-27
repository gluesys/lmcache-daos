<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# 재사용 통계 — 하위 계층(tiering)을 둘지 판단하는 숫자

축출한 KV 청크를 버리지 않고 더 싼 곳(DAOS HDD 풀, 외부 S3)으로 내리는 tiering 은 **축출한 청크가 다시
찾아질 때만** 이득이다. 레인 부하로는 이 빈도를 알 수 없어, 실서비스·PoC 에서 커넥터가 직접 센다.

## 켜기

| 환경변수 | 기본 | 뜻 |
|---|---|---|
| `DAOS_REUSE_STATS` | 0(끔) | 초 단위 기록 주기. 그 주기마다 한 줄 로그 |
| `DAOS_REUSE_STATS_GHOST` | 100000 | 기억하는 축출 키 수(오래된 것부터 잊음) |

비용은 저장된 청크마다 dict 항목 하나(키 + 숫자 3 개)와 축출 키 기억이다. 요청 경로에서는 잠금 한 번과
dict 연산 몇 개뿐이다. daos-operator 차트는 `vllm.services[].reuseStats` 로 켠다.

in-process 커넥터만 센다. MP 모드는 LMCache 의 L2 축출이 키를 고르므로 아직 연결하지 않았다.

## 로그

`DAOS reuse stats {...}` 한 줄, 시작 이후 누적 JSON:

| 필드 | 뜻 |
|---|---|
| `writes`, `write_bytes` | 저장한 청크 수·바이트 |
| `hits`, `hit_bytes` | 읽은 청크 수·바이트. `hits_unknown` 은 이 프로세스가 쓰지 않은(다른 프로세스·이전 기동) 청크 |
| `misses` | 조회가 비어 있던 횟수. LMCache 는 요청마다 첫 빈 청크에서 조회를 멈추므로 **요청당 한 번** |
| `ghost_misses`, `ghost_miss_bytes` | **이 프로세스가 축출한 청크가 다시 찾아진 횟수**. 하위 계층이 있었다면 재계산 대신 hit 이 되었을 요청 |
| `ghost_miss_age` | 축출부터 다시 찾기까지의 시간 분포 |
| `rewrites_after_evict` | 축출한 청크를 재계산해 다시 쓴 횟수 |
| `evictions`, `evicted_bytes`, `reads_at_evict` | 축출 수·바이트, 축출될 때까지 읽힌 횟수 분포 |
| `hit_gap` | hit 마다 직전 접근(쓰기·읽기)부터의 시간 분포 |
| `reads_live` | 지금 남아 있는 청크의 읽힌 횟수 분포 |

청크는 객체 경로에서 dkey 하나(layerwise 는 층마다가 아니라 청크로 한 번), DFS 경로에서 파일 하나다.

## 판단

1. **`ghost_misses / misses`** — 놓친 요청 중 하위 계층이 살렸을 몫. 0 에 가까우면 tiering 은 쓸모없다
   (놓치는 것은 처음 보는 프롬프트다). 이 값이 크면 2 로.
2. **`ghost_miss_age`** — 하위 계층에 얼마나 오래 둘지. 대부분 10 분 안이면 DAOS 용량을 늘리는 편이
   단순하고, 몇 시간 뒤에 돌아오면 HDD·S3 계층이 맞다.
3. **손익분기** — 청크 하나를 하위 계층에서 가져오는 시간(지연 + 바이트 ÷ 대역폭)이 그 청크까지의
   prefill 재계산보다 짧아야 한다. Qwen2.5-1.5B 에서는 1,280 토큰(35 MiB) 재계산 0.37 s, DAOS 읽기 47 ms
   (2026-10-24) — 이 모델에서는 S3(0.1~0.3 s 추정)의 이득이 작다. 큰 모델·긴 문맥일수록 커진다.
4. **`reads_at_evict`** — 축출되는 청크 대부분이 `<=0`(한 번도 안 읽힘)이면, 내릴 대상은 S3-FIFO 의
   주 큐 꼬리(한 번 이상 읽힌 것)로 좁힌다(EVICTION-DESIGN.md §4.3).

`ghost_misses` × (재계산 시간 − 하위 계층에서 가져오는 시간) ÷ `elapsed_s` 가 하위 계층이 초당 아껴 줄
GPU 시간이다. 재계산·가져오기 시간은 그 요청들의 문맥 길이로 정한다.
