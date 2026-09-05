# KV-cache 벤치마크 A·B 재측정 — LMCache MP 모드 + DAOS L2 어댑터 vs Hub 문서(개정 16)

작성 2026-09-04. 비교 대상: Hub 문서 **"KV-cache 벤치마크 A·B·C (개정 16)"**
(`cfa646ed-3b9e-4f1a-94e7-828ac0f98dd5`, 2026-08-25~29 측정). 같은 하네스(`bench/sweep2.py` 방식의 고정 토큰열·KV
비례 settle, `bench/longdocqa.py`)를 **MP 모드 스택**에서 다시 돌렸다. 브랜치 `mp-mode`, 런처 `deploy/launchers/run_vllm_mp_c5.sh`.

## 0. 먼저 알아야 할 차이 (수치를 나란히 놓기 전에)

| | Hub 문서 (2026-08) | 이번 측정 (2026-09-04) |
|---|---|---|
| 클라이언트 | client-6 (H100 NVL, Xeon 8558 ×2, 400GbE) — 반납됨 | **client-5** (H100 NVL, SNC 4 NUMA, 400GbE) |
| LMCache 경로 | **in-process** `LMCacheConnectorV1` + `DaosConnector`(RemoteConnector) | **MP 모드**: 별도 캐시 서버(L1 pinned 100 GB) + `DaosL2Adapter` |
| LMCache `c_ops` | **미적재**(휠 CUDA 13 ↔ torch cu128, 08-31 정정 공지) → Python 폴백 | 적재(소스 빌드 이미지 `kvsup-ucx-lmc`) |
| DAOS | 2.8(ExaStor), ucx+rc_v, 랭크당 NVMe **8 대 — 양 랭크가 같은 드라이브 8 대를 공유(오구성)** | 2.9.100 stockfull, ucx+rc_v, 랭크당 **4 대, 랭크 간 분리** |
| 데이터 정합성 | 공유 드라이브로 조용한 손상이 있던 상태(정정 공지 §1) | 손상 0, 게이트 PASS |
| 컨테이너 | RP_2G4/S16, chunk 4 MiB | S16, chunk 4 MiB, rd_fac:0, MP 어댑터는 `root:/mp*` 아래 raw 페이로드 40 MiB/객체 |
| 측정 방식 | miss → settle → hit (한 프로세스) | 같은 방식(**L1 warm**) + **재시작 후 hit**(L1 소거 → DAOS 경로, `bench/sweep_mp.py`) |

즉 Hub 문서의 DAOS 열은 "in-process + c_ops 없음 + 손상 상태" 의 값이고, 이번 값은 "MP + c_ops + 정상 스토리지" 다.
두 열의 차이를 전부 MP 의 공로로 읽으면 안 된다. 어제(09-03) 같은 client-5 에서 **in-process + c_ops + 정상 스토리지**로
잰 hit 가 8K 151~221 ms, 16K 251~444 ms(`deploy/README.md` §10.1)로 Hub 문서의 151/298 과 같은 자릿수였으므로,
in-process 경로의 기준선은 Hub 값으로 봐도 큰 오차가 없다.

MP 의 "hit" 는 두 종류다. **L1 warm**: 저장 직후 같은 서버 프로세스에서의 hit(pinned L1 에서 GPU 로). **DAOS**: 서버를
재시작해 L1 을 비운 뒤의 hit(DAOS → L1 → GPU). 기본 프리페치 정책(`--l2-prefetch-policy default`)에서는 L2 에서 가져온
객체를 L1 에 남기지 않아 재시작 뒤에는 **두 번째 요청도 DAOS 읽기**다. 그리고 프로세스 기동 후 **첫** load 는 전송 워밍업으로
14~27 GB/s 밖에 못 내고(두 번째부터 31~34 GB/s), 첫 요청 값에 그 비용이 들어 있다. 표에서는 첫/둘째를 따로 적는다.

## 1. Part A — 컨텍스트 스윕 (hit TTFT, ms)

Qwen3-14B, chunk_size 256, 64K/127K 는 YaRN(MML 66560/1.625, 131072/3.2). 컨텍스트별 KV 는 1.34 / 2.68 / 5.20 / 10.74 / 21.43 GB.

| ctx | recompute Hub / MP | Hub CPU cache | Hub 로컬 SSD RAID | **Hub DAOS (in-proc)** | **MP L1 warm** | **MP DAOS (2번째)** | MP DAOS (기동 후 1번째) |
|---|---|---|---|---|---|---|---|
| 8K | 570 / 685 | 102 | 297 | 151 | **99** | **103** | 238 |
| 16K | 1325 / 1293 | 91 | 455 | 298 | **88** | **170** | 179 |
| 31K | 3083 / 2951 | 149 | 847 | 437 | **142** | **313** | 319 |
| 64K | 8621 / 8386 | 332 | 1825 | 1125 | **319** | **701** | 788 |
| 127K | 25062 / 24855 | 575 | 3538 | 2129 | **554** | **1182** | 1482 |

recompute 대비 배수:

| ctx | Hub DAOS | Hub CPU cache | **MP DAOS** | **MP L1** |
|---|---|---|---|---|
| 8K | 3.8× | 5.6× | **6.6×** | **6.9×** |
| 16K | 4.4× | 14.6× | **7.6×** | **14.7×** |
| 31K | 7.1× | 20.7× | **9.4×** | **20.8×** |
| 64K | 7.7× | 26.0× | **12.0×** | **26.3×** |
| 127K | 11.8× | 43.6× | **21.0×** | **44.9×** |

실효 대역폭(KV ÷ hit TTFT, 고정 오버헤드 포함 하한):

| ctx | Hub DAOS | **MP DAOS** | 비 | Hub CPU cache | **MP L1** |
|---|---|---|---|---|---|
| 8K | 8.9 GB/s | **13.0** | 1.47× | 13.1 | 13.5 |
| 16K | 9.0 | **15.8** | 1.75× | 29.5 | 30.5 |
| 31K | 12.0 | **16.6** | 1.40× | 35.2 | 36.6 |
| 64K | 9.5 | **15.3** | 1.60× | 32.3 | 33.7 |
| 127K | 10.1 | **18.1** | 1.80× | 37.3 | 38.7 |

읽는 법:
- **MP 의 DAOS 경로가 Hub 의 in-process DAOS 보다 전 구간 1.4~1.8× 빠르다.** 127K 에서 2129 → 1182 ms. 어댑터의 DAOS→L1
  읽기 자체는 31~34 GB/s(로그 `load task: 20440 MiB in 633 ms = 33.8 GB/s`)로 DAOS 상한이고, 남는 시간은 L1→GPU 와
  스케줄링이다. 이 두 단계가 직렬인 것은 in-process 와 같다(§7.2 of `MP-MODE-PLAN.md`).
- **MP 의 L1 warm hit 는 Hub 의 "CPU cache" arm 과 같은 값**(99/88/142/319/554 vs 102/91/149/332/575)이다. 즉 MP 는
  in-process 의 로컬 CPU 캐시 속도를 그대로 내면서, 그 L1 이 **인스턴스 간 공유되고 DAOS 로 뒷받침**된다(§3). Hub 문서가
  "CPU cache 는 최속이나 대안이 아니다"(노드 로컬·RAM 종속·재시작 소실) 라고 한 세 제약이 MP 구조에서 풀린다:
  공유는 §3, 용량은 L1 을 넘는 만큼 DAOS 가 받고(§2 의 L1=20 GB 런), 재시작 뒤에도 DAOS 에서 다시 채운다(DAOS 열).
- 기동 후 첫 요청은 전송 워밍업이 붙어 8K 238 ms 처럼 튄다. 운영에서는 기동 시 프로브가 흡수하고(어댑터가 4 KiB 프로브를
  쓰고 읽음), 남는 것은 첫 대용량 전송의 워밍업이다.

## 2. Part B — long-doc-qa (100 GB working set, 12 inflight, 149 쿼리)

149 docs × 4096 tok, seed 42, populate 후 30 s settle. Hub 와 동일 코퍼스(`longdocqa.py` 고정 시드).

| arm | avg TTFT | p50 | p95 | wall | 집계 | vs recompute(Hub) |
|---|---|---|---|---|---|---|
| Hub recompute | 4334 ms | 3714 | 6768 | 56.7 s | 1.76 GB/s | — |
| Hub 로컬 SSD RAID | 1074 | 1128 | 1145 | 13.7 s | 7.30 | 4.0× |
| **Hub DAOS (in-proc)** | 371 | 356 | 547 | 4.7 s | 21.36 | 11.7× |
| **MP DAOS — 재시작 후 질의만(100 GB 전부 DAOS 에서)** | **281** | **259** | **433** | **3.6 s** | **27.92** | **15.4×** |
| MP L1=20 GB (working set 이 L1 의 5 배) | 296 | 234 | 909 | 3.8 s | 26.46 | 14.6× |
| MP L1=100 GB (L1 상주) | **202** | 205 | 236 | 2.6 s | **38.31** | 21.5× |

- populate(prefill + store): Hub 63 s ↔ MP 59 s(1.70 GB/s). store 경로가 병목이 아닌 것은 같다.
- **DAOS 경로 12 inflight 집계 27.9 GB/s** 는 Hub 의 21.36 보다 1.31× 높고, Hub 가 2 노드 동시에 도달했던 서버 천장(32.8)에
  단일 클라이언트로 가깝다. 146 개 load 태스크가 동시에 뛰어 태스크당 평균 11.8 GB/s(합이 상한).
- L1=20 GB 런은 MP 의 실제 운영 형태(L1 < working set)다: 145 요청 중 122 가 L2(DAOS), 23 이 L1 에서 왔다. avg 는
  DAOS 콜드와 같은데 **p95 가 909 ms 로 벌어진다** — L1 eviction 과 L2 load 가 겹치는 구간의 꼬리. 튜닝 대상
  (`--eviction-trigger-watermark`, `--l2-prefetch-max-in-flight`).
- L1 상주(100 GB)는 38.3 GB/s — DAOS raw 상한을 넘는다. 이 값은 스토리지가 아니라 pinned→GPU 경로의 값이다.

## 3. Part C — 멀티노드 공유

Hub 의 Part C(client-6 → client-7 크로스노드 149/149 히트, 444 ms)는 GPU 노드가 하나(client-5)뿐이라 재현하지 못했다.
대신 **같은 노드의 두 vLLM 인스턴스가 한 MP 서버를 공유**하는 형태(`run_vllm_mp2_c5.sh`)를 측정했다(`MP-MODE-PLAN.md` §7.3):
A 가 저장한 8K/16K KV 를 B 가 첫 요청에서 **125 / 140 ms** 로 받았고(공유 L1, `41 L1, 0 L2`), 재시작 뒤에는 DAOS 에서
264 ms(16K). 크로스**노드**는 MP 서버가 노드마다 하나씩이므로 L1 이 아니라 DAOS 를 통해 공유되며, 그 값은 §1 의 "MP DAOS" 열이다
(Hub 의 크로스노드 444 ms 는 4K 문서 12 inflight 조건 → §2 의 281 ms 와 대응).

## 4. 결론

1. **DAOS 경로 자체가 1.3~1.8× 빨라졌다**(Part A 1.4~1.8×, Part B 1.31×). 기여는 세 가지가 섞여 있고 분리 측정은 하지
   않았다: MP 의 zero-copy L1 적재(어댑터가 DAOS 를 pinned 버퍼에 직접 읽음), `c_ops` 적재, 공유 드라이브 오구성 제거.
   in-process + c_ops 재측정(09-03)이 Hub 값과 같은 자릿수였으므로 **MP 구조의 몫이 크다**고 보는 것이 맞다.
2. **MP 의 L1 은 Hub 의 CPU cache 와 같은 속도**(8K 99 ms, 127K 554 ms)이며, Hub 가 CPU cache 를 대안에서 제외한 이유
   (공유 불가·용량·소실)를 구조적으로 해소한다. 이것이 in-process 대비 MP 를 고를 실질 근거다.
3. Hub 문서 §7-4a 의 "병목이 매체를 떠나 LMCache retrieve 경로로 이동했다(11.4~11.7 GB/s 수렴)" 는 MP 에서도 성질이 같다.
   어댑터는 34 GB/s 를 내는데 end-to-end 는 13~18 GB/s 다. 남은 레버는 여전히 **L2 load 와 L1→GPU 의 겹침**이고, 그것은
   프리페치 컨트롤러가 요청당 load 태스크를 하나만 내는 LMCache 서버 쪽에 있다(어댑터에서 불가).
4. Hub 문서의 DAOS 수치를 인용할 때는 08-31 정정 공지대로 "c_ops 없음 + 손상 상태" 를 병기해야 하고, 정상 구현의 대표값으로는
   이 문서의 MP 열(또는 09-03 in-process 재측정)을 쓰는 것이 맞다.

## 5. 재현

```
# Part A (MML 32768; 64K/127K 는 MML=66560 FACTOR=1.625 / MML=131072 FACTOR=3.2 로 재기동)
ARM=mp CTXS=8192,16384,31744 TAG=sweepA MODE=store python3 bench/sweep_mp.py
/root/run_vllm_mp.sh   # 재시작 = L1 소거
ARM=mp CTXS=8192,16384,31744 TAG=sweepA MODE=hit   python3 bench/sweep_mp.py
# Part B
L1_GB=100 ROOT=/mp_ldq100 /root/run_vllm_mp.sh; ARM=mp_l1_100 POPULATE=1 python3 bench/longdocqa.py
L1_GB=100 ROOT=/mp_ldq100 /root/run_vllm_mp.sh; ARM=mp_cold  POPULATE=0 python3 bench/longdocqa.py
L1_GB=20  ROOT=/mp_ldq20  /root/run_vllm_mp.sh; ARM=mp_l1_20  POPULATE=1 python3 bench/longdocqa.py
```
어댑터 `root` 를 런마다 바꾸는 이유: 같은 코퍼스를 다시 populate 하면 DAOS 에 남은 객체가 히트가 되어 populate 가 무효가 된다
(Hub §7-4 표의 한계 ① 과 같은 함정).

## 6. `ofi+verbs;ofi_rxm` 재측정 (2026-09-05) — 갱신된 비교 표

전송을 `ucx+rc_v` 에서 `ofi+verbs;ofi_rxm` 으로 바꾼 뒤(스톨 제거, `doc/MP-MODE-PLAN.md` §7.7d) Part A·B 를 같은 절차로 다시 돌렸다.
어댑터 DAOS→L1 읽기가 39~40 GB/s 로 UCX(33~37)보다 높고, store 직후 첫 load 의 15 s 스톨이 없어 "재시작 후 첫 요청" 도 정상값이다.

### 6.1 Part A — hit TTFT (ms), Qwen3-14B, verbs

| ctx | recompute | Hub DAOS (in-proc) | Hub CPU cache | **MP L1 warm** | **MP DAOS 콜드 (기동 후 1번째 / 2번째)** |
|---|---|---|---|---|---|
| 8K | 705 | 151 | 102 | **105** | **139 / 97** |
| 16K | 1287 | 298 | 91 | **85** | **158 / 163** |
| 31K | 2942 | 437 | 149 | **141** | **281 / 370** |
| 64K | 8382 | 1125 | 332 | **315** | **596 / 649** |
| 127K | 24773 | 2129 | 575 | **553** | **1204 / 1076** |

| ctx | MP DAOS vs Hub DAOS | recompute 대비 (MP DAOS) | 실효 BW MP DAOS | Hub DAOS |
|---|---|---|---|---|
| 8K | 1.1× | 5.1× | 9.6 GB/s | 8.9 |
| 16K | 1.9× | 8.1× | 17.0 | 9.0 |
| 31K | 1.6× | 10.5× | 18.5 | 12.0 |
| 64K | 1.9× | 14.1× | 18.0 | 9.5 |
| 127K | 1.8× | 20.6× | 17.8 | 10.1 |

(콜드 = 기동 후 첫 요청 기준. 31K 의 2번째가 370 인 것은 그 load 가 23.6 GB/s 로 느렸던 1회 잡음이다. 스트리밍은 off.)

### 6.2 Part B — 100 GB working set, 12 inflight, 149 쿼리

| arm | avg TTFT | p50 | p95 | 집계 | populate |
|---|---|---|---|---|---|
| Hub recompute | 4334 ms | 3714 | 6768 | 1.76 GB/s | — |
| Hub 로컬 SSD RAID | 1074 | 1128 | 1145 | 7.30 | — |
| Hub DAOS (in-proc) | 371 | 356 | 547 | 21.36 | 63 s |
| MP DAOS 콜드, ucx (09-04) | 281 | 259 | 433 | 27.92 | 59 s |
| **MP DAOS 콜드, verbs — 재시작 후 100 GB 전부 DAOS** | **216** | **210** | **295** | **36.19** | **42 s** |
| MP L1=20 GB, verbs (working set 5 배) | 230 | 211 | 443 | 33.93 | 42 s |
| MP L1=20 GB, verbs, **inflight 6** | 131 | 127 | 151 | 30.39 | — |
| MP L1=100 GB 상주, verbs | 204 | 206 | 232 | 37.96 | 42 s |

- DAOS 콜드 경로가 Hub 대비 avg **1.72×**, 집계 **1.69×**(21.4 → 36.2 GB/s). Hub 가 2 노드 동시에 도달했던 서버 천장(32.8 GB/s)을
  **단일 클라이언트가 넘는다** — 콜드 DAOS 36.2 는 이제 어댑터 읽기 상한(39~40)의 90% 다.
- populate 63 → 42 s: 쓰기도 verbs 에서 빠르다(2.37 GB/s, prefill 포함).
- L1 < working set 의 p95 꼬리(443)는 §7.6 대로 큐잉이며 inflight 6 에서 151 ms 로 사라진다(처리량 30 GB/s 유지).

### 6.3 한 줄 정리
verbs 전환으로 MP 의 DAOS 경로는 Hub 문서 대비 Part A 1.1~1.9×, Part B 1.7×, 스톨 없음. 남는 격차는 in-process 시절부터 같은
L1→GPU 직렬 구간이며, 그것은 §7.7 의 스트리밍(현재 프로토타입, 기본 off)이 다룬다.
