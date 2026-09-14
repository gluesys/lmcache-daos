<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# 저수준 객체 API 전환 — 1단계 계획

## 왜

객체당 고정비가 **46배** 차이난다 (`doc/LAYERWISE-MEASUREMENT.md:185`).

```
object API : t ≈ 0.0137 ms + 0.0385 ms/MiB     (26   GB/s 한계)
DFS        : t ≈ 0.63   ms + 0.067  ms/MiB     (15.7 GB/s 한계)
```

고정비 비중은 객체 크기가 정한다.

| 객체 크기 | DFS 에서 고정비 | 전환 이득 |
|---|---|---|
| 1 MiB | **90 %** | 46배에 가까움 |
| 4 MiB | 70 % | 큼 |
| 16 MiB | 37 % | 1.7배로 수렴 |

레이어 스트리밍이 DFS 에서 진 이유가 이것이다. 레이어별로 쪼개면 객체가 작고
많아지는데 DFS 는 객체 하나당 0.63 ms 를 그냥 낸다. 최선끼리 비교해도 464 ms 대
269 ms 로 1.7배 뒤졌고, 따라잡으려면 객체당 0.056 ms 가 필요한데 당시 0.696 ms
였다 — **12배 부족**이었다. 저수준은 그 12배를 한 번에 넘긴다.

장애 거동도 더 낫다. `doc/FAILURE-MODES.md` 에서 잰 대로 저수준 **블로킹**은 DFS 와
똑같이 고착하지만(16/16), 이벤트 큐를 쓰면 스레드를 전부 되찾는다(0/16). DFS 에는
그 선택지가 없다. 400G verbs 에서 처리량 손실도 없다(31.04 대 30.64 GB/s, 분산 안쪽).

## 지금 상태

서빙하는 세 경로는 **전부 DFS** 다. NIXL 플러그인만 저수준이고, 그건 처음부터
그랬다.

| 경로 | DFS 호출 | 저수준 호출 |
|---|---|---|
| in-process 커넥터 | 12 | 0 |
| MP L2 어댑터 | 15 | 0 |
| GDS 백엔드 | 17 | 0 |
| NIXL 플러그인 | 0 | 7 |

## 범위 — 1단계는 커넥터 하나만

세 경로를 한 번에 뒤집지 않는다. 셋이 **같은 `DfsSys` 표면**을 쓰므로, 커넥터에서
검증된 백엔드를 나머지가 그대로 받아쓸 수 있다. 검증되지 않은 것을 세 곳에 동시에
넣을 이유가 없다.

1단계에서 **바꾸지 않는 것**: MP 어댑터, GDS 백엔드, NIXL 플러그인, 기존 컨테이너의
데이터, 어떤 기본값도.

## 이음매

세 경로가 `DfsSys` 에 부르는 것은 17개뿐이고, 대부분 그대로 대응된다.

| DFS | 저수준 | |
|---|---|---|
| `open_rdwr_create` / `open_rdonly` / `close_obj` | dkey 계산만 | 핸들 개념이 사라짐 |
| `write_obj_from` / `read_obj_into` | `daos_obj_update` / `daos_obj_fetch` | 직결 |
| `stat_size` / `exists` | `iod_size` 조회 | 쉬움 |
| `remove` | `daos_obj_punch_dkeys` | 쉬움 |
| `write_gpu_from` / `read_gpu_into` | `*_gpu` + `mem_attrs` | 이벤트도 받는다 (확인됨) |
| `listdir` | `daos_obj_list_dkey` | 형태가 다름 |
| `mkdir_p` / `lookup` / `move` / `release` | **대응 없음** | 아래 참고 |

### `move` 가 사라지는 것은 오히려 이득이다

GDS 백엔드는 임시 이름으로 쓰고 **원자적 rename** 으로 절단을 막는다
(`gds_backend.py:361-368`). dkey/akey 에는 rename 이 없다. 대신 메타 akey 와
페이로드 akey 를 **한 RPC 에 같이 쓰면** 원자성이 공짜로 따라온다. 지금은 쓰기가
두 번(헤더 → 페이로드)이고 그 사이에 크래시하면 절단 객체가 남는데, 저수준에서는
그 창이 아예 없다.

## 키 매핑 — 여기서 결정이 갈린다

`CacheEngineKey` 의 필드는 이것뿐이다.

```
model_name, world_size, worker_id, chunk_hash, dtype, request_configs, tags
```

**프리픽스 식별자가 없다.** `chunk_hash` 는 청크마다 다르고, 키 하나만 보고
"이들이 같은 요청에 속한다"를 알아낼 방법이 없다. 쓰기는 `batched_put` 이 목록을
통째로 받으니 묶을 수 있지만, **읽기가 안 된다** — 나중에 캐시 히트로 읽는 쪽은
키만 갖고 있지 원래의 묶음을 모른다.

따라서 설계는 이렇게 된다.

```
oid   = 컨테이너당 하나 (또는 model/worker 당 하나)
dkey  = sha256(key.to_string())        <- 청크 하나
akey  = "M" (메타) + "P" (페이로드)
        레이어 모드에서는 "M" + "L000".."L039"
```

이 매핑이 주는 것과 주지 않는 것을 분명히 해 둔다.

| | |
|---|---|
| **얻는다** | 객체당 고정비 0.63 → 0.0137 ms. 청크 수가 많을수록 크다 |
| **얻는다** | **레이어 접기.** 한 청크의 레이어들이 dkey 를 공유하므로 40개를 1 RPC 로 보낸다. NIXL 에서 2.4배를 낸 바로 그것이고, 이 그룹핑은 키에서 유도된다 |
| **못 얻는다** | **청크 간 접기.** 프리픽스 식별자가 없어 불가능하다. 상류 문제이며 LMCache 이슈 #5090 과 같은 계열이다 |

레이어 접기가 가능하다는 점이 중요하다. 1단계의 이득은 대부분 여기서 나온다.

## 모드 선택 — 플래그가 아니라 컨테이너가 정한다

컨테이너 타입은 **생성 시점에 고정된다.** POSIX 컨테이너에 `daos_obj_update` 를
쓸 수도, 비-POSIX 를 `dfs_sys_connect` 할 수도 없다. 둘 다 실제로 부딪혔다.

```
test_torn_object → nixltest : container is not of type POSIX
test_connector   → nixltest : dfs_sys_connect rc=22
```

그러므로 모드는 **데이터가 어디 있느냐**가 정한다. 같은 데이터를 두 모드로 번갈아
읽는 것은 불가능하고, 온-디스크 형식도 다르므로 in-place 마이그레이션도 없다.

설정 플래그보다 **컨테이너에서 자동 판별**이 낫다.

```
daos cont query → layout_type
  POSIX    → DfsSys 백엔드   (지금과 동일, 기본)
  unknown  → Raw 백엔드
```

이유가 둘이다. 첫째, 플래그와 컨테이너가 어긋나면 **20분간 99.8 % CPU 로 도는
`DER_HG` 반복**으로 나타난다 — 400G 측정 때 한 시간을 여기에 썼고, 원인을
알아보기 어려운 형태다. 둘째, 운영자는 컨테이너를 만들 때 이미 결정을 내렸다.
같은 결정을 설정 파일에 또 적게 할 이유가 없다.

명시 플래그(`?mode=raw`)는 자동 판별을 **덮어쓰는 용도**로만 두고, 어긋나면 즉시
거절한다.

## 산출물

| | 내용 |
|---|---|
| `lmcache_daos/obj_binding.py` | `daos_obj_*` ctypes 바인딩. `DfsSys` 와 같은 17개 표면 |
| `lmcache_daos/serde_v3.py` | akey 분리 형식. v1/v2 와 공존 |
| `connector.py` | 컨테이너 타입 판별 + 백엔드 선택. DFS 가 기본 |
| `tests/test_torn_object_raw.py` | 절단 7형태를 dkey/akey 로 다시 증명 |
| `tests/test_raw_roundtrip.py` | 왕복 + 정합성 |
| `doc/RAW-API-MEASUREMENT.md` | 레이어 모드에서 DFS 대 raw |

## 반드시 통과해야 하는 것

새 백엔드는 **기존 게이트를 그대로 통과해야 한다.** 형식이 다르다고 기준을 낮추지
않는다.

| 게이트 | 왜 |
|---|---|
| `tests/test_torn_object.py` 의 7형태 | 절단이 miss 로 읽혀야 한다. 헤더-먼저-쓰기가 사라지므로 **다시 증명해야 한다** |
| `tests/kv_correctness_gate.sh` | 저장이 양자화·손실 없이 왕복하는가 |
| `tests/kv_failure_rate.sh` | 히트가 틀린 KV 를 주는 비율. A/B 는 기준율 없이는 무의미하다 |
| `tests/failure_modes.py` | 이벤트 큐 시한이 저수준에서도 동작하는가 |

## 함정 — 미리 적어 둔다

**`rd_fac`.** 풀 기본값으로 컨테이너를 만들면 `rd_fac=1` 이고, 저수준 경로는
`DER_HG(-1020)` 를 15초마다 반복하며 영원히 멈춘다. 동작하는 컨테이너는 전부
`rd_fac=0` 이다. 컨테이너 생성 헬퍼가 **`--properties=rd_fac:0` 을 강제해야 한다.**
400G 측정에서 이것에 한 시간을 썼고, 증상이 플러그인 결함처럼 보인다.

**객체 클래스.** 랭크 수에 맞아야 한다. 1랭크 풀에서 `S16` 은
`daos_obj_generate_oid` 가 `DER_INVAL` 로 거절한다. `SX` 가 무난하다.

**이벤트 큐는 빌려 쓴다.** 스레드당 하나로 만들면 놀고 있는 EQ 가 network context
를 쥐어 느려진다 — cxl2 에서 블로킹보다 35 % 느렸다. `nixl/plugin/daos_backend.cpp`
의 `nixlDaosEqPool` 이 같은 문제의 해법이고, 파이썬 쪽도 같은 형태여야 한다.

## 순서

1. `obj_binding.py` + `serde_v3.py`, DAOS 없이 도는 단위 시험부터
2. 컨테이너 타입 판별과 백엔드 선택. **DFS 경로는 손대지 않는다**
3. 절단 7형태를 저수준에서 통과
4. 왕복·정합성 게이트 통과
5. 레이어 모드에서 DFS 대 raw 측정 — **여기서 이득이 실증되지 않으면 중단한다**
6. 결과를 보고 MP·GDS 확대 여부 결정

5번이 관문이다. 46배는 마이크로벤치의 고정비 차이이고, 엔진 오버헤드가 섞인
실제 경로에서 얼마가 남는지는 **아직 측정되지 않았다**. 남지 않으면 POSIX 검사
가능성과 절단 보호를 내주고 얻는 것이 없다.
