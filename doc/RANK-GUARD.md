<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# rank 가드 — 저장 노드가 재부팅되는 동안 캐시가 멈추지 않게

`DAOS_RANK_GUARD=1` 로 켜는 raw 객체 경로의 장애 격리 계층이다. 이 문서는
운영자가 알아야 할 것만 적는다. 왜 이런 모양인지는 `lmcache_daos/rank_guard.py`
머리말과 FlexA Hub `1f09478a` §2.1~§2.9 에 있다. 아래 실측을 다시 내는 시험
스크립트는 `bench/reboot/` 에 있다.

## 1. 무엇을 해결하나

DAOS 는 `self_heal:none` 풀에서 rank 하나가 내려가도 아무것도 실패시키지 않는다.
그 rank 로 간 요청은 **끝나지 않고** 60 s 마다 재전송되고, `daos_event_abort`
는 요청을 끝내지 않으며(먼저 버퍼를 풀면 프로세스가 죽는다), rank 가 돌아오면
전부 rc 0 으로 완료된다 — abort 했던 put 도 그때 반영된다. 가드가 없으면 vLLM
의 워커 스레드가 하나씩 그 rank 에 붙들려 살아 있는 2/3 의 캐시까지 멈춘다.

가드는 다음을 한다.

| 조각 | 역할 |
|---|---|
| 기한 | 요청마다 `base + 바이트 / min_bw`. 읽기 0.5 s, 쓰기·punch 10 s 가 base |
| zombie 보관 | 기한을 넘긴 요청의 이벤트·버퍼를 DAOS 가 완료할 때까지 들고 있는다 |
| breaker | rank 별(EC 버킷이면 풀 단위) 닫힘/열림/반열림. 기한 초과 2 회에 열리고, probe 간격은 5 s 에서 두 배씩 `DAOS_RANK_GUARD_MAX_OPEN`(60 s)까지. rc 0 으로 회수된 zombie 는 "rank 가 응답했다"로 보고 즉시 닫는다 |
| 이벤트 큐 | rank 마다 하나, poller 스레드는 하나. 열린 rank 의 큐는 `max(50 ms, 10 × 직전 poll 시간)` 간격으로만 본다 |
| put-if-absent | 메타 akey 가 있으면 쓰지 않는다(키는 내용 해시) |
| 축출 | `DAOS_RANK_GUARD_CAPACITY_GB` 가 있으면 프로세스 단위 근사 LRU. 기동 때 버킷 dkey 를 나열해 색인을 복원하고 1 s 마다 오래된 dkey 를 punch 한다 |

받을 수 없는 get 은 **miss** 로 끝난다(LMCache 가 재계산). 저장 못 한 put 은
메타데이터를 쓰지 않고 버린다.

## 2. 설정

```
DAOS_RANK_GUARD=1
DAOS_RANK_GUARD_OCLASS=S1          # rd_fac 0 풀. EC 풀이면 EC_2P1G1
DAOS_RANK_GUARD_CAPACITY_GB=<풀의 1/3 이하>
DAOS_RANK_GUARD_BUCKETS=64         # 컨테이너마다 고정. 바꾸면 키가 다른 버킷으로 가 miss
DAOS_RANK_GUARD_DEADLINE=0.5       # 읽기 base(s)
DAOS_RANK_GUARD_WRITE_DEADLINE=10  # 쓰기·punch base(s)
DAOS_RANK_GUARD_MIN_BW=50e6        # 기한에 더할 크기/대역폭(B/s)
DAOS_RANK_GUARD_MAX_OPEN=60        # breaker 열림 상한(s)
DAOS_RANK_GUARD_RELOCATE=0         # 1: 기본 rank 가 열린 키를 다른 rank 버킷에(S1 만, RANK-GUARD-RELOCATION.md)
DAOS_RANK_GUARD_RELOCATE_BUDGET_GB= # 꺼진 rank 하나 몫으로 재배치할 상한, 기본 CAPACITY_GB 의 1/4(§6.1)
```

LMCache 설정은 **플러그인 경로만** 쓴다. `remote_url` 과 `remote_storage_plugins`
를 함께 주면 LMCache 0.5.5 가 같은 컨테이너에 RemoteBackend 를 두 개 만든다.

```yaml
chunk_size: 256
remote_serde: "naive"
remote_storage_plugins: ["daos"]
extra_config:
  remote_storage_plugin.daos.url: "plugin://daos/<pool>/<container>?sys=<name>"
  remote_storage_plugin.daos.module_path: lmcache_daos.connector
  remote_storage_plugin.daos.class_name: DaosConnector
```

vLLM 은 `--kv-transfer-config` 에 `"kv_load_failure_policy":"recompute"` 를 넣는다.
기본 `fail` 이면 조회와 읽기가 엇갈린 요청(기한 초과, 공간 부족)이 HTTP 500 이 된다.

컨테이너는 비 POSIX(`daos cont create <pool> <cont>`, 타입 지정 없음). 풀은
daos-operator 의 `spec.profile: kvcache` 로 만들면 아래 조건이 맞춰진다.

MP 모드는 `--l2-adapter` JSON 에 `"rank_guard": true` 와 `guard_*` 를 쓴다. 위 환경변수와
1:1 이다: `guard_oclass`, `guard_buckets`, `guard_deadline`, `guard_write_deadline`,
`guard_min_bw`, `guard_max_open`, `guard_relocate`. 용량은 `max_capacity_gb` 가 맡는다.
LMCache 의 L2 축출(`max_capacity_gb` + `eviction`)이 가드의 삭제 경로를 부른다.

## 3. 풀 두 가지와 고르는 기준

| | rd_fac 0 + `self_heal:none` + 가드 | EC 2+1(기본 self_heal) + 가드 |
|---|---|---|
| 노드 240 s 정지 중 | 그 노드 블록은 miss(TTFT ≈ 재계산 0.66 s), 최악 1.3 s | hit 유지(p50 0.18 s). 정지 직후 80~90 s 는 재계산 수준 miss |
| 복귀 | Joined 6 s 뒤 바로 hit | DAOS 가 rebuild(약 2 분) 뒤 `dmg pool reintegrate` 필요 |
| 장애 중 새 pool connect | 불가(rank 복귀까지). 가드가 접속을 백그라운드로 돌려 vLLM 은 정상 시간에 기동하고, 복귀 약 7 s 뒤 캐시가 켜진다(그 전엔 전부 miss) | 가능 |
| 장애 중 공간 회수 | **멈춤**(아래 4절) | 계속 |
| 공간 | 1 배 | 1.5 배 + rebuild 여유 |
| 노드 | 제한 없음 | 3 개 이상 |

1.5 배 공간과 rebuild·reintegrate 운영을 감당할 수 있으면 EC + 가드, 아니면
rd_fac 0 + 가드. 가드 없는 EC 는 정지 직후 40~60 s 동안 요청당 3.7 s 가 걸려
권하지 않는다.

### rd_fac 0 풀의 self_heal 과 exclude

rd_fac 0 풀에서 rank 의 target 이 한 번이라도 제외되면 컨테이너가 UNCLEAN 이 되고
모든 I/O 가 DER_RF 로 실패한다. 막는 것은 풀 `self_heal:none` 하나다.

- DAOS 2.8 은 rank 사망으로 풀 target 을 제외할 때 시스템 self_heal 의 `pool_exclude`
  와 풀 self_heal 의 `exclude` 를 **둘 다** 요구한다. 풀이 `none` 이면 시스템
  self_heal 과 엔진 `DAOS_POOL_RF` 를 바꾸거나 점검할 필요가 없다. `DAOS_POOL_RF` 는
  제외를 거부할 수만 있다. 같은 시스템의 다른 풀은 기본 self_heal 을 그대로 쓴다.
- **장애 중에 풀 self_heal 을 `exclude` 로 바꾸지 않는다.** 장애 동안 보류된 사망
  이벤트가 그 자리에서 처리돼 target 이 제외될 수 있다(소스 근거, 미실측). rank 가
  Joined 로 돌아온 뒤 바꾸는 것은 제외를 일으키지 않았다(실측).
- 재부팅만 하는 rank 를 `dmg system exclude` / `dmg pool exclude` 하지 않는다.
- NVMe 장치가 고장으로 판정되면 DAOS 는 self_heal 과 관계없이 그 엔진의 **모든 풀**
  에서 해당 target 을 제외한다. rd_fac 0 풀은 이때 UNCLEAN 이 된다. 캐시이므로
  컨테이너를 다시 만드는 것이 복구다.

## 4. 용량 상한을 잡는 법

- 물리 사용은 논리 상한의 **2.5~3 배**로 돌았다(회수 지연, 64 버킷의 target 편차).
- `self_heal:none` 풀은 rank 하나가 내려가 있는 동안 **살아 있는 모든 rank 의
  VOS aggregation·GC 가 멈춘다**(daos_metrics 로 확인). punch 하든 덮어쓰든 공간이
  돌아오지 않고, rank 가 다시 합류한 뒤 한꺼번에 돌아온다.
- 그러므로 상한은 **풀의 1/3 이하**에서 시작하고, `예상 장애 시간 × 쓰기 속도`
  만큼 여유를 더 둔다. 13 GB 풀에 3 GiB 상한은 정상 때는 충분했고 4 분 장애에는
  부족했다(DER_NOSPACE 270 회, 요청 실패는 0).

## 5. 실측(레인 A, 3 노드 IB, DAOS 2.8.0, vLLM 0.30 + LMCache 0.5.5, A2 16 GB)

| run | 구성 | 장애 중 반복 p50 | 최악 | 복귀 | 실패 |
|---|---|---|---|---|---|
| K | in-process, rd_fac 0 + 가드 | 0.655 s(재계산 0.73) | 1.28 s | Joined + 6 s | 0/4200 |
| P | MP 모드, rd_fac 0 + 가드 | 0.562 s | 1.06 s | 1 분 안 | 0/3800 |
| O | EC 2+1, 가드 없음 | 0.171 s, 단 정지 직후 요청당 3.7 s | 10.9 s | reintegrate 2.3 분 | 0 |
| T | EC 2+1 + 가드 | 0.180 s, 정지 직후 90 s 는 0.78 s | 1.8 s | reintegrate 필요 | 0/3200 |

## 6. 한계

- 축출 색인은 프로세스 단위다. 인스턴스 하나에 컨테이너 하나를 전제한다. 여러
  프로세스가 한 컨테이너를 공유하면 MP 모드를 쓴다.
- 기동 전에 layerwise 로 쓰인 데이터는 시작 스캔에 잡히지 않는다.
- EC 풀에서 정지 직후의 miss 구간(80~90 s)은 DAOS 가 degraded 로 다시 빨라지는
  시간이 정한다. breaker 상한을 줄여도 짧아지지 않는다.
- 재부팅한 rank 가 다시 합류한 직후 SWIM 이 다시 dead 로 판정해 Excluded 로
  남는 일이 18 회 중 2 회 있었다(DAOS 쪽, 엔진 파드 재시작으로 복구). 풀 destroy
  가 `ds_pool_stop` 대기로 고착되는 일도 있었다. 둘 다 상류 보고 초안이 있다.
