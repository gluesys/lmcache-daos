<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# LMCache layerwise 모드 실측 (2026-09-11, client-5)

## 요약

`use_layerwise: True` 는 우리 `DaosConnector` 에 **코드 수정 없이 붙는다.** 적중률도 100% 다.
그런데 같은 `chunk_size` 에서 적중 TTFT 가 **11.5배 느리다.** 바이트는 동일하고 객체 수만
레이어 수(40)배로 늘어나기 때문이다.

원인은 대역폭이 아니라 **객체당 약 0.63 ms 의 고정 비용**이다. 객체 크기를 세 가지로 바꿔
직선을 맞춰 분리했고, 워커 수를 바꿔도 변하지 않음을 확인했다.

한 가지는 layerwise 가 이긴다. **콜드 미스 TTFT 가 절반이다.** 저장이 레이어 계산과 겹친다.

## 환경

client-5, Qwen3-14B(40 레이어, KV 헤드 8, head_dim 128, bf16), `--max-model-len 32768`,
`--enforce-eager --no-enable-prefix-caching`, DAOS 2.9.100 stockfull, pool `attr1`,
컨테이너 `kvlmc5`(S16, chunk 4 MiB, rd_fac:0), 전송 `ofi+verbs;ofi_rxm`,
에이전트 domain `mlx5_0`, `local_cpu: false`, IO 스레드풀 16(명시하지 않은 경우).
하네스는 `bench/sweep2.py`(고정 토큰열, `prompt_token_ids` 전송).

`chunk_size: 256` 에서 레이어 하나의 청크는 정확히 1 MiB 다
(256 토큰 × 8 헤드 × 128 dim × 2(K,V) × 2 B). 전체 레이어를 합치면 40 MiB.

## 적중 TTFT (ms)

| ctx | base/256 | base/1024 | lw/256 | lw/1024 | lw/4096 |
|---|---|---|---|---|---|
| 8192 | 166 | **145** | 982 | 340 | 270 |
| 16384 | 176 | **159** | 1843 | 595 | 330 |
| 30720 | 290 | **269** | 3343 | 1075 | 464 |

`lw/256` 은 세 컨텍스트 모두 `sweep2.py` 의 `hit < miss/2` 게이트를 통과하지 못해
INVALID 로 표시됐다. 캐시가 안 맞은 것이 아니라(로그는 `Retrieved 30720 out of 30720`),
적중이 재계산 대비 2배도 못 줄인 것이다.

## 미스 TTFT (ms) — 여기서는 layerwise 가 이긴다

| ctx | base/256 | base/1024 | lw/256 | lw/1024 | lw/4096 |
|---|---|---|---|---|---|
| 8192 | 883 | 1391 | 1275 | 785 | **620** |
| 16384 | 2379 | 2854 | 2344 | 1419 | **1338** |
| 30720 | 5579 | 5756 | 4462 | 2976 | **2837** |

30720 에서 `base/1024` 5756 ms → `lw/4096` 2837 ms 로 **2.03배** 빠르다.
저장이 레이어별로 일어나 전방 계산과 겹치기 때문이다. 비layerwise 는 forward 가 끝난 뒤
한꺼번에 저장하므로 첫 토큰이 그만큼 밀린다.

## 고정 비용 분리

30720 기준, 유효 객체당 시간 = 벽시계 시간 / 객체 수 (워커 16 포함된 값).

| arm | 객체 수 | 객체 크기 | 벽시계 | 객체당 |
|---|---|---|---|---|
| lw/256 | 4800 | 1 MiB | 3343 ms | 0.696 ms |
| lw/1024 | 1200 | 4 MiB | 1075 ms | 0.896 ms |
| lw/4096 | 280 | 16 MiB | 464 ms | 1.657 ms |

세 점이 직선에 잘 맞는다.

```
t_eff(size) ≈ 0.63 ms + 0.067 ms/MiB
```

1 MiB 와 4 MiB 두 점으로 맞춘 뒤 16 MiB 를 예측하면 1.70 ms 가 나오고 실측은 1.657 ms 다.

기울기를 대역폭으로 환산하면 15.7 GB/s 다. **데이터를 옮기는 능력은 정상이다.**
문제는 절편이다. 1 MiB 객체에서는 시간의 **90%가 고정 비용**이다.

| 객체 크기 | 고정 | 이동 | 고정 비중 |
|---|---|---|---|
| 1 MiB | 0.63 ms | 0.07 ms | 90% |
| 4 MiB | 0.63 ms | 0.27 ms | 70% |
| 16 MiB | 0.63 ms | 1.07 ms | 37% |

## 워커를 늘리면 오히려 나빠진다

`lw/256` 을 IO 스레드풀 16 과 128 로 각각 측정했다.

| ctx | 16 워커 | 128 워커 |
|---|---|---|
| 8192 | 982 ms | 1084 ms |
| 16384 | 1843 ms | 2009 ms |
| 30720 | 3343 ms | 3660 ms |

8배를 늘렸는데 9~10% 느려졌다. 고정 비용이 **동시성으로 감춰지지 않는 직렬 구간**이라는 뜻이다.
`connector.py` 의 P4 주석에 적힌 관찰(per-EQ `eqx_lock` 이 submit+completion 을 직렬화해
큐 배치와 무관하게 7~12 GB/s 에서 막힘)과 같은 방향이다.

이 실험을 위해 하드코딩된 `self._workers = 16` 을 `DAOS_WORKERS` 환경변수로 열었다.
기본값은 그대로 16 이라 기존 동작은 변하지 않는다.

## 차단(blocking) 경로가 비차단 경로보다 빠르다

`base/*` 는 `batched_get`(차단), `lw/*` 는 `batched_get_non_blocking` 을 탄다.
같은 모델로 base 를 예측하면 실측이 일관되게 21~27% 빠르다.

| arm | 객체 크기 | 예측 | 실측 | |
|---|---|---|---|---|
| base/256 | 40 MiB | 3.31 ms | 2.42 ms | 27% 빠름 |
| base/1024 | 160 MiB | 11.4 ms | 8.97 ms | 21% 빠름 |

비차단 경로의 asyncio 래핑 비용으로 보인다. 이는 `support_batched_get_non_blocking()` 을
False 로 둔 기존 판단(2026-08-25)을 다시 뒷받침한다.

## 상류 버그: layerwise 해제 경로의 이중 free

`lw/*` 실행 중 메모리 오브젝트마다 다음 경고가 쏟아진다.

```
Ref count of MemoryObj <addr> is negative: -1.
Double free occurred somewhere. Setting ref count back to 0 as a hack but please ...
```

LMCache 0.5.2 `retrieve_layer` 의 정리 구간이 `ref_count_down()` 을 중복 호출한다.
`dev` 에도 같은 구조가 남아 있다(루프에서 `ref_count_down()` 을 돌린 뒤 다시 `unpin()`
루프를 돈다). 상류 보고 대상이다. 기능은 동작하므로 성능 수치에는 영향이 없어 보이나
검증하지 않았다.

## 판정

- **읽기 위주 KV 재사용에는 layerwise 를 켜지 말 것.** 어떤 청크 크기에서도 기본 모드를
  이기지 못한다. 최선끼리 비교해도 464 ms 대 269 ms 로 1.7배 뒤진다.
- 지금 설계(모든 레이어를 한 객체에)는 DAOS 에서 **옳은 선택**이다. 고정 비용을 분산시킨다.
  layerwise 는 정확히 그 이점을 되돌린다.
- **콜드 미스 지연이 중요한 워크로드라면 재고 가치가 있다.** 2배는 작지 않다.
  다만 그 경우에도 `chunk_size` 를 1024 이상으로 올려야 한다.

## 하드웨어 요구사항으로의 환산

`lw/256` 이 `base/1024`(269 ms)를 따라잡으려면 4800 객체를 269 ms 에 처리해야 하므로
객체당 0.056 ms 가 필요하다. 현재 0.696 ms 이니 **약 12배** 줄여야 한다.

즉 "레이어 스트리밍이 이기려면 객체 조회 고정 비용이 12배 싸져야 한다" 가 이 측정의
정량적 결론이다. DPU 쪽 KV 인덱스 아이템의 근거 수치로 쓸 수 있다.

## 재현

```bash
# client-5 (cell1 경유)
CHUNK=1024 DAOS_WORKERS=16 bash /root/launch.sh layerwise
podman exec -e ARM=lw_c1024 -e CTXS=8192,16384,30720 vllm-daos \
    python3 /cfg/bench/sweep2.py
```

`launch.sh` 는 `deploy/launchers/run_vllm_perf_c5.sh` 에서 파생했고 `MODE`(base|layerwise),
`CHUNK`, `DAOS_WORKERS` 를 받는다.
