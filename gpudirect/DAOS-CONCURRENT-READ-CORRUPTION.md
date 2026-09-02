# DAOS 읽기 데이터 손상 — 최종 검토 결과, 상류 제출용 분석, 세션 인계

최종 검토 2026-09-01. 근거 커밋 `ee8439b` (브랜치 `streaming-get-and-client6-assets`).
재현기 `tests/dfs_integrity.c`, 보조 `tests/dfs_integrity_ab.sh`·`tests/agg_ab.sh`.
상세 서술 `README.md`, 배포 정보 `../deploy/MANIFEST.md`.

| | |
|---|---|
| **판정** | **DAOS 자체 결함.** 우리 패치·커넥터·LMCache·GPU-direct prereq 전부 측정으로 배제됨 |
| **영향 버전** | **완전 upstream 2.9.100 스택(코어+prereq+클라 전부 스톡)에서 재현 확정(§18).** ExaStor 2.8-wsd·2.9-gds 빌드도 동일 |
| **증상** | 읽기 버퍼의 **정확히 DFS chunk 하나**가 **다른 객체의 같은 오프셋 데이터**로 조용히 바뀜 |
| **비율** | 28 MiB · 16 스레드 mixed 부하에서 읽기당 **0.3 ~ 1.5 %** (버스트로 몰려서 옴) |
| **탐지 가능성** | 반환값·크기 정상 → 호출자는 알 수 없음. 컨테이너 checksum 을 켜면 **EIO 로** 표면화 |
| **상류 보고** | **같은 서명의 보고 없음.** 가장 가까운 DAOS-18862 는 "Cannot Reproduce" 로 종결 |
| **결론** | **이 백엔드는 사용 불가 유지.** 조용히 틀린 KV 를 서빙한다 |

## 0. 최종 검토 결과

### 0.1 측정으로 확정한 것

| # | 확정 사실 | 근거 |
|---|---|---|
| 1 | **DAOS 결함이다.** 코어·mercury·UCX 를 `--build-deps=yes` 로 전부 스톡 빌드한 클라이언트가 패치 클라와 **동률로 실패**: 34/3840(0.885 %, CI 0.634–1.235) vs 25/3840(0.651 %, CI 0.441–0.959), 런 단위 교대 A/B | §12.1 |
| 2 | **완전 upstream 스택에서 재현.** 스톡 2.9.100 서버(코어+mercury/UCX/SPDK prereq 전부 upstream, WS-D 0)+스톡 클라, ofi+tcp: DFS 86/5120, raw obj 59/1920, 서명 동일 | **§18** |
| 2b | rc3 기반 ExaStor 2.8-wsd 서버도 동일(§13 — 단 upstream 이 아니라 wsd 브랜치였음, §13 정정 참조) | §13 |
| 3 | **서명**: 정확히 DFS chunk 하나가 **같은 오프셋의 다른 객체 데이터**(가끔 같은 객체의 다른 오프셋, 드물게 이전 세대, 드물게 zeros). 구간 크기는 **컨테이너 chunk 크기를 따라감**(1 MiB 컨테이너 → 1 MiB) | §12.2 |
| 4 | **저장된 바이트는 정상**이다. 같은 런에서 조용히 되읽으면 깨끗 ⇒ **읽기가 저장되지 않은 바이트를 돌려준다** | §12.2 |
| 5 | **서버가 스스로 손상을 검출하고 있다.** 양 rank 가 `vos_csum_recalc.c:csum_agg_verify()` 에서 `DER_CSUM(-2021)`, 실패 창이 정확히 4 MiB. NVMe 는 Media/Read/Write Errors **0** 인데 DAOS Checksum Errors 2~6 ⇒ 하드웨어 아님 | §12.5 |
| 6 | **checksum 은 해결책이 아니라 검출기**다. `cksum:crc32` 컨테이너에선 조용한 손상 대신 **EIO** 가 난다 | §12.6 |
| 7 | **부수 증상 2건**: 지속 부하에서 쓰기가 `DER_MISC(-1025)`→EIO(서버측 **bulk 핸들 역직렬화 실패**가 원인), 컨테이너 close 마다 **DTX CoS flush 실패** | §12.7·§12.8c |

### 0.2 측정으로 기각한 것 (가설 묘지)

| 기각된 가설 | 어떻게 기각됐나 |
|---|---|
| 우리 패치·커넥터·LMCache·GPU-direct prereq | 완전 스톡 클라가 동률 실패 (§12.1) |
| **VOS aggregation** | arm 당 5120 읽기 + 순서 교대. 1차 1.50 % vs 0.53 % 로 확정처럼 보였으나 순서를 뒤집으면 1.44 % vs 1.33 %. 끄고도 78/8960 = 0.87 % (§12.8) |
| oclass·복제·컨테이너 신선도 | RP_2G1/G4/G8·SX(rd_fac 0)·S1 전부 재현, 갓 만든 컨테이너도 6/1920 (§12.4) |
| payload chunk 정렬 | 교대 A/B 5120×2 에서 0.37 %(정렬) vs 0.68 %(straddling) — 완화책 아님 (§13.5) |
| 2.9 신규 도입 / GPU-direct 백포트 | 2.8-wsd 에서 동률 재현 (§13.3) |
| **ExaStor 패치 전체 (서버 포함: WS-D·zfs-cap·GDS cart)** | **완전 upstream 서버+클라에서 동일 재현 (§18)** — §17.1 서로소 논증의 잔여 구멍까지 봉합 |
| 하드웨어 미디어 오류 | 전 장치 Media/Read/Write Errors 0 (§12.5) |
| DAOS-15847·DAOS-18901 (상류 fetch/aggregation 수정) | 2.9.100 엔 있고 2.8-rc3 엔 없는데 비율이 같은 자릿수 (§14.6) |

### 0.3 남은 미해결 (다음 세션의 일)

1. **근본 원인 미규명.** 남은 유력 방향은 **서버측 fetch/bulk 경로** — §12.8c 의 `hg_bulk_deserialize` 실패와 같은 뿌리일 가능성.
2. **DAOS-19569**(2026-08-31, affects 2.8·3.0, Awaiting backport): "multiple IODs" IOM 처리 오류, 본문에 *"possibly cause data corruption"*. 28 MiB 읽기는 정확히 multi-IOD 케이스다. component 가 EC 로 적혀 있어 RP 경로 해당 여부 확인 필요 (§14.4).
3. **`UCX_ENABLE_RCACHE=n`** 미시험. DAOS-18862 보고자가 양쪽에서 끈 상태였다 = UCX 등록 캐시를 의심했다는 뜻 (§14.3).
4. **§12.8b**(단일 스레드 덮어쓰기 세대만으로 재현)를 **건강한 풀에서** 결론내기. 열화된 풀에서 관측된 것이고, 2.8 에서도 포맷 직후엔 심했다가 치유됐다(§13.4).
5. ~~완전 상류 vanilla 서버 미검증~~ → **§18 에서 해소. 이제 상류 제출을 막는 것이 없다.**

### 0.4 이 조사에서 얻은 측정 규칙 (이걸 어기면 또 틀린다)

- **실패는 버스트로 온다.** 같은 arm 안에서도 블록별 0.16 %~3.75 % 로 20배 흔들린다. ⇒ **풀 읽기수 기준 이항(Wilson) 신뢰구간은 arm 비교에 쓸 수 없다**(과산포).
- **arm 비교는 런 단위 교대**로 한다. 블록으로 묶어야 하면 **순서를 뒤집은 대조**를 함께 돌린다.
- **검증 페이로드는 위치·객체를 식별하는 태그**여야 한다. 상수 채움·난수 한 덩어리는 "같은 객체의 다른 오프셋" 유형을 원리적으로 못 본다(과거 "2.8+UCX 무결 30/30" 오판의 절반이 이것 — §13.6).
- **30~200 읽기로 판정하지 않는다.** 1 % 결함은 30회를 74 %, 80회를 45 % 확률로 통과한다.
- 이 규칙을 어겨서 이 세션에서도 두 번 틀렸다: "S1 은 면역"(0/2560 → 2/1920), "aggregation 이 원인"(순서 뒤집으니 소멸).

### 0.5 문서 읽는 순서

| 목적 | 볼 곳 |
|---|---|
| 결론만 | §0 (여기) |
| 재현 | §2 |
| 상류 제출 초안 | §0.1 + §2 + §12.2 + §12.5 + §13 + §14 |
| 다음 작업 | §0.3 + §10 |
| 환경 인계 | §13.7(현재 = 2.8) + §7(2.9 시절 기록) + §8 운영 함정 |
| 역사·철회 기록 | §11 + §12.11 + §13.4 |

⚠️ **§4·§5·§7 은 2026-08-31 이전 기록이다.** §4 의 스톡 대조는 §12.1 이 대체하고, §5 의 서명은
모호한 패턴으로 얻은 것이라 §12.2 가 대체한다. §7 의 환경은 서버가 2.8 로 바뀌기 전 상태다.

---

## 1. 한 문장

**DAOS 2.8.0-rc3 · 2.9.100 에서, 크게 덮어쓰이는 객체를 읽으면 읽기 버퍼의 정확히 DFS chunk
하나가 다른 객체의 같은 오프셋 데이터로 조용히 바뀐다.** 28 MiB 객체 · 16 스레드 쓰기+읽기
혼합에서 읽기당 0.3~1.5 %. 반환값과 크기는 정상이므로 호출자가 알 수 없다.

(초판은 이것을 "동시 읽기" 문제로 적었으나 부정확하다 — 읽기만 동시에 해서는 나오지 않고
쓰기가 섞여야 한다(§12.3). 다만 덮어쓰기 세대를 쌓으면 단일 스레드로도 관측된 적이 있다(§12.8b).)

## 2. 재현

**필요한 것: DAOS 클라이언트뿐.** LMCache·GPU·torch·모델·Python 전부 불필요.

```bash
gcc -O2 -pthread -o dfs_integrity tests/dfs_integrity.c \
    -I$DAOS/include -L$DAOS/lib64 -ldfs -ldaos -ldaos_common -lgurt -lm \
    -Wl,-rpath,$DAOS/lib64

# 컨테이너: POSIX, RP_2G4, chunk 4 MiB, rd_fac:1
daos cont create <pool> <cont> --type=POSIX \
    --file-oclass=RP_2G4 --dir-oclass=RP_2G4 --oclass=RP_2G4 \
    --chunk-size=4194304 --properties=rd_fac:1

# 기본 arm: 16 스레드 × 40 라운드 = 640 읽기, 보통 2~15 건 손상
NA_UCX_EXTRA_TLS= ./dfs_integrity -p <pool> -c <cont> -s 28 -t 16 -r 40
```

출력은 손상마다 그 조각의 **출처를 이름으로** 말한다:

```
CORRUPT t8  r9  first bad word at 25165784: object t7's data (round 9, offset 25165784)
                | words ok=3145727 zero=0 foreign-obj=524288 stale-round=0 shifted=1 junk=0
                | retry=clean
PASS/FAIL: 64/640 concurrent reads corrupt (10.00%, 95% CI 7.91-12.57%) ...
```

arm 분리 플래그(한 번에 하나만 바꿀 때):

| 플래그 | 의미 | 기대 |
|---|---|---|
| (기본) | 쓰기+읽기 혼합 | 0.3~1.5 % 손상 |
| `-W -Q -V 3` | 조용한 단일 스레드 쓰기 + 조용한 검증 3회 | 깨끗 (건강한 풀에서) |
| `-W -M` | 조용히 쓴 뒤 **읽기만** 동시 | 깨끗 (0/4320) |
| `-N -V 3` | **쓰기만** 동시 + 조용한 검증 | 깨끗 |
| `-t 1` | 단일 스레드, 단일 객체 | 깨끗 (0/150) |
| `-o 0` / `-o 36` | payload 정렬 / straddling | 0.37 % / 0.68 % (§13.5) |
| `-Q` | 기존 객체만 감사(쓰기 없음) | 남아 있는 객체의 상태 확인용 |

**한 번의 PASS 는 결함 부재가 아니다** — §0.4 의 측정 규칙을 먼저 읽을 것. arm 을 비교하려면
`tests/dfs_integrity_ab.sh`(런 단위 교대 + Wilson 구간)를 쓴다.

원본 Python 판(`tests/test_rawio_integrity.py 28 16 13 hdr burst`)은 기록으로 남긴다. 패턴이
모호해 "다른 객체" 와 "같은 객체의 다른 오프셋" 을 구분하지 못하므로 신규 측정에는 쓰지 말 것.

## 3. 배제된 것 (다시 검증하지 말 것)

| 후보 | 배제 근거 |
|---|---|
| 우리 커넥터의 zero-copy read (MemoryObj alias) | 목적지를 전용 `bytearray` 로 바꾸면 **더 나빠짐** (1.9% → 6.2%) |
| LMCache 전체 | LMCache 를 임포트하지 않는 순수 DFS 테스트에서 재현 |
| LMCache MemoryObj 수명/참조 계수 | 위와 동일. 참조 반납 수정도 효과 없음 (85%→70%, 겹침) |
| 우리 저장소의 특정 커밋 | 같은 호스트에서 main·브랜치·alias 세 코드가 30~40% 로 구분 안 됨 |
| `daos-0002` (TSE_TASK_ARG_LEN 840→968) | `D_CASSERT` 로 컴파일 타임 검증되고, 변경은 여유를 늘리는 방향 |
| **`daos-0004` (rkey 스텁) 및 draft 패치 전체** | **스톡 2.9.100 코어(`841487de8`)에서도 재현** — §4 |
| `dfs_sys` 핸들 캐시 | `DFS_SYS_NO_CACHE` 에서도 재현 (4.3% → 1.9%, 겹침) |
| DFS chunk 정렬 / 헤더 36 B straddling | 페이로드 오프셋 0·4 MiB 정렬에서도 재현. 2026-09-01 교대 A/B 로 재확인: 0.37 %(정렬) vs 0.68 %(straddling) — 완화책 아님 — §13.5 |
| 목적지 버퍼 타입 | `bytearray` 와 torch 백업 메모리 양쪽 재현 |
| 4 MiB chunk 크기 자체 | 32 MiB chunk 컨테이너에서도 재현(더 심한 형태) |
| **완전 스톡 prereq (mercury/UCX 포함)** | **2026-09-01 에 측정으로 배제됨 — §12.1.** 남은 마지막 변수였고, 이제 없다. |
| oclass·복제 | RP_2G1/G4/G8·SX(rd_fac 0)·S1 전부에서 재현 — §12.4 |
| 클라이언트 읽기 동시성 자체 | 조용히 쓴 객체를 16 스레드로 읽기만 하면 **0/4320** — §12.3 |
| **VOS aggregation** | `reclaim:disabled` 로 꺼도 78/8960 = 0.87 %. 순서 교대로 효과 소멸 — §12.8 |
| 2.9 에서 새로 생긴 결함 / GPU-direct 백포트 | **2.8.0-rc3 에서 동률 재현** — §13.3 |
| 하드웨어 미디어 오류 | 전 NVMe 가 Media/Read/Write Errors **0**, DAOS 내부 csum 카운터만 증가 — §12.5 |
| 컨테이너 신선도 | 갓 만든 컨테이너도 6/1920 — §12.4 |

## 4. 스톡 대조 실험 (초판 — **§12.1 이 대체**)

⚠️ 이 실험은 두 arm 이 **패치된 mercury/UCX 를 공유**했다. 그 변수를 닫은 것이 §12.1 이며,
현재의 핵심 증거는 §12.1 이다. 아래는 기록으로 남긴다.

같은 저장소에서 GPU-direct API 도입 커밋 `133e6f8ca` 의 **부모 `841487de8`**
(`v2.9.100-tb` 태그가 조상)를 별도 prefix 로 빌드하고, prereq 는 패치본을 그대로 복사해
재사용했다. 두 arm 의 차이는 **DAOS 코어 라이브러리 하나뿐.**

| | A: 패치 | B: 스톡 |
|---|---|---|
| `libdaos.so.2.8.0` 크기 | 8950616 | **8922384** |
| `libcart.so.4` → `HG_Bulk_import_rkey` | 1 | **0** |
| 버전 | 2.9.100 | 2.9.100 |
| 실패 / 208 | 2 (1.0%) | **4 (1.9%)** |

각 arm 이 실제 로드한 `libdaos` 크기를 출력해 번들 교체를 확인했다.

## 5. 손상 서명 (2026-08-31 이전 관측 원자료 — §12.2 가 대체)

⚠️ 이 절은 모호한 바이트 패턴으로 얻은 초기 기록이다. **현재 서명은 §12.2**(태그 페이로드).
아래의 "오프셋·크기 모두 가변" 판정도 일부는 패턴 모호성 때문이었다.

`payload_off=36` 이므로 **파일 오프셋 = 표시된 페이로드 위치 + 36**.

| 페이로드 위치 | 파일 오프셋 | 4 MiB 배수? |
|---|---|---|
| 4194268 | 4 MiB | 예 |
| 8388572 | 8 MiB | 예 |
| 12582876 | 12 MiB | 예 |
| 16777180 | 16 MiB | 예 |
| 20971484 | 20 MiB | 예 |
| 25165788 | 24 MiB | 예 |
| 3145728 | ~3 MiB | **아니오** |
| 6291420 | 6 MiB | **아니오** |
| 7339996 | 7 MiB | **아니오** |
| 11534300 | 11 MiB | **아니오** |
| 2572288 | ~2.45 MiB | **아니오** |
| 0 | 0 | — |

손상 크기(4 KiB 페이지 단위 표본): **116, 256, 396, 512, 628, 1024, 2048, 7168**
→ 약 0.45 MiB ~ 28 MiB(객체 전체).

버퍼 내용:
- **대개** 버퍼 앞부분은 올바른 스레드/키의 패턴 → 객체 중간이 깨진다
- **때때로** 다른 스레드/키의 패턴 → 동시 실행 중인 다른 읽기의 데이터 혼입
- **드물게** 객체 **전체**가 다른 스레드의 데이터 (7168/7168)
- **때때로** 어느 패턴도 아님 (`unknown`) → 미기록 영역의 잔여 내용으로 보임

⚠️ 초기 판에서 "정확히 4 MiB chunk 하나가 chunk 경계에서 유실" 이라고 특성화했으나
**표본을 늘리자 오프셋과 크기 모두 가변**이었다. 철회했다. 상류 제출 시 "chunk 경계" 로
단정하지 말 것.

## 6. 검정력 — 이것이 왜 지금까지 안 보였나

읽기당 ~1% 결함은 소규모 시험을 그냥 통과한다:

- 순차 30회 전부 통과 확률 `0.99³⁰ ≈ 74%`
- 80회 전부 통과 확률 `0.99⁸⁰ ≈ 45%`

그래서 Hub 문서 `DAOS KV-cache over RoCE v4`(id `0bf8fe7d-…`)의 **"무결성 30/30"** 과
이 저장소의 초기 **"PASS 80/80"** 둘 다 **결함의 부재를 보인 것이 아니다.** 그 문서에
정정 댓글을 달았다(§9).

또한 순차 대 동시 비교(각 200/208 검사)에서 순차 0%, 동시 2.9% 였다 — 동시성이 방아쇠다.
다만 이후 실행에서 순차(`loop` 모드)도 실패했으므로 **순차가 안전하다는 뜻은 아니다.**

**교훈: 208 검사로도 arm 간 구분이 안 되는 경우가 있었다. 비율을 비교하려면 수백 단위
표본과 신뢰구간이 필요하다.** `tests/kv_failure_rate.sh` 가 Wilson 구간을 출력한다.

**2026-09-01 보강 — 표본 수만으로는 부족하다.** 실패는 버스트로 오고(블록별 0.16~3.75 %),
그래서 **풀 읽기수 기준 이항 신뢰구간 자체가 무효**다(과산포). arm 당 5120 읽기를 모아도
순서만 뒤집으면 결론이 뒤집혔다(§12.8). 규칙은 §0.4 로 정리했다.

## 7. 환경 (2.9 시절 기록 — **현재 상태는 §13.7**)

⚠️ 2026-09-01 에 서버를 2.8.0-rc3 으로 되돌렸다(§13.2). 아래는 그 전 상태이며, 접속 경로와
운영 함정은 그대로 유효하다.

접속: `ssh tta1` → cell1 (116.89.174.82:20022). client-* 는 cell1 을 릴레이로 접속.
**client 노드끼리 직결 SSH 없음.**

| 호스트 | 역할 | 비고 |
|---|---|---|
| cell1 / cell2 | DAOS 서버 rank 0/1 | `/opt/daos-gds` 실행중, provider `ucx+rc_v` |
| client-5 | **작업 장비** | 전체 스택 준비됨, 다른 사용자 없음 |
| client-6 | vLLM 스택 | **다른 사용자가 CXL 작업 중** — 재시작 시 조율 필요 |

client-5 준비 상태:
- `/root/lmcache-daos-br` (브랜치 코드), `/root/lmcache-daos` (main 기반 코드)
- `/root/daoslibs29` (패치 DAOS 클라이언트 번들), `/root/daoslibs-stock` (**스톡** 번들)
- `/opt/daos-stock` (스톡 2.9.100 설치본), `/opt/daos-gds-gpu` (패치)
- `localhost/kvsup:052` 이미지, `/home/hf/hf_cache` 모델
- ⚠️ podman 스토리지가 `/home/containers` **bind mount** — **재부팅 시 해제됨**
  (루트 69 GB 로는 22 GB 이미지 로드 피크 44 GB 를 못 버팀)
- CDI 생성됨 (`nvidia.com/gpu=all`)

cell1 빌드 트리:
- `/var/daosbuild/daos-gds` — draft `theodore/b_cufile` `c87080a70`, **패치가 워킹트리 변경**
- `/var/daosbuild/daos-stock` — 사본, `841487de8` (스톡)
- ⚠️ **두 트리가 빌드 디렉터리를 공유**(`/var/daosbuild/build-gpu`). 패치 버전을 재빌드하려면
  패치를 다시 적용해야 한다.

CI 클러스터 192.168.35.40/41/42 (`root`, 자격증명은 사용자가 세션에서 제공):
**대조군으로 쓰지 말 것.** provider 가 `ofi+verbs;ofi_rxm` — v4 문서가 "대용량 RDMA read
를 조용히 손상시킨다" 고 특정해 UCX 로 전환한 그 provider다. 여기서 재현되면 RxM 버그를
본 것이고 "스톡에서도 재현 → 우리 패치 무죄" 라는 거짓 결론이 된다. 추가로 VM(QEMU
NVMe·zvol), `targets: 1`, rank 0·3 Excluded, `daos_server` 전부 inactive, 클라이언트/서버
빌드 불일치(151.g4d2012d79 vs 340.g31214ce07).

## 8. 운영 함정 (전부 실제로 겪음)

1. **`daos_server` 재시작마다 SPDK wedge.** `device_unplugged` +
   `load blobstore failed -1025` 가 재현되고, bdev 전체 wipe → `setup.sh reset` → 재기동 →
   format 을 거쳐야 복구된다. format 은 **풀을 파기**한다. → **서버 설정 변경을 요구하는
   실험을 설계하지 말 것.**
2. **`NA_UCX_EXTRA_TLS=` 를 비워 둘 것.** 패치된 mercury 는 `cuda_copy,cuda_ipc` 를 TLS 에
   넣을 수 있고, CUDA 가 로드 가능하면 **호스트 메모리 전송이 깨진다**(별개 결함, 이미 수정:
   패치 기본값을 opt-in 으로 반전). 라이브러리 순서로 우회하지 말 것.
3. **클라이언트 번들은 서버 빌드와 맞춰야 한다.** DAOS 는 버전과 무관하게
   `libdaos.so.2.8.0` 이라는 파일명을 쓰므로 불일치가 보이지 않는다.
   `deploy/check_manifest.sh` 로 검증(크기+심볼, 비영점 종료).
4. **`daos` CLI 는 client-5 의 PATH 에 없다.** 컨테이너 생성/파기는 **cell1 에서** 할 것.
5. **컨테이너 destroy 실패를 확인할 것.** vLLM 이 열고 있으면 실패하고, 다음 create 가
   `DER_EXIST` 로 막힌다. 클라이언트를 먼저 정지.
6. **출력을 `/dev/null` 로 버리지 말 것.** 이 세션에서 네 번, 그 때문에 실패 원인을 놓쳤다
   (이미지 로드, 컨테이너 초기화, client-5 컨테이너 생성, destroy).
7. **`podman save | podman load` 는 이미지 크기의 약 2배 피크 공간**을 쓴다(스테이징 tar +
   레이어).
8. **파이프라인 중간 `ssh` 가 heredoc stdin 을 삼킨다.** 수신측에 `-n` 을 붙이면 파이프가
   끊긴다. 그리고 tar 스트림 앞에 정보 출력을 섞으면 아카이브가 깨진다.

## 9. 문서 정정 상태

- Hub `0bf8fe7d-…` (`DAOS KV-cache over RoCE v4`) — 댓글 2건: 손상 발견, 그리고
  "무결성 30/30" 의 검정력 부족
- Hub `cfa646ed-…` (개정 16), `5c944988-…` (GPUDirect 검증) — 각 댓글 1건
- Hub `719497ff-…` — 정정 공지 문서(마크다운, 수정 가능). **현재 "원인 미해결" 로 되어
  있으므로 갱신 필요. 갱신 내용: DAOS 결함 확정(스톡 동률), 2.8.0-rc3 도 동일, checksum 은
  검출기일 뿐, 상류 선행 보고 없음(DAOS-18862 가 최근접·재현불가 종결).** — 미완료

## 10. 작업 순서 (최종 검토 기준, 우선순위대로)

1. **DAOS-19569 확인** — `git fetch origin` 후 패치 diff 를 보고 RP 경로 해당 여부 판단.
   해당되면 재현기로 전후 비교(그 티켓이 원인이면 조사가 끝난다). §14.4
2. **`UCX_ENABLE_RCACHE=n`** 을 서버·클라 양쪽에 걸고 런 단위 교대 A/B. DAOS-18862 의 힌트다.
   §14.3
3. **§12.8b 결론내기** — 건강한 풀(테스트 컨테이너 정리 후)에서 갓 만든 컨테이너에 단일 스레드
   덮어쓰기 세대를 1→30 까지 올리며 세대별 실패율 기록. 성립하면 상류 재현기가 단일 스레드로
   단순해진다.
4. **손상 시점 서버 로그** — 런타임 `dmg server set-logmasks` 로 `DD_SUBSYS=vos,bio,object`
   (재시작 불필요), 클라이언트는 `D_LOG_MASK=ERR`. §12.8c 의 bulk 역직렬화 실패와 대조.
5. **상류 제출** — JIRA `daosio.atlassian.net` 에 신규 티켓(§14.1 의 API 로 검색·확인 가능).
   포함: §0.1 확정 목록, §2 재현 절차와 C 파일, §12.2 서명 원문, §12.5 서버 로그 + 장치 카운터,
   §13.3 버전 비교표, §12.6 checksum 거동, §12.7 부수 증상. **DAOS-18862 를 참조**하고
   "그 티켓의 재현기를 제공한다"는 프레임으로 낼 것.
6. **Hub 정정 공지 갱신**(§9).
7. **chunk 크기 재확인**(512 KiB·16 MiB)으로 "손상 구간 = chunk 크기" 를 한 번 더 못박기.

**그때까지 이 백엔드는 사용 불가로 유지한다.** 조용히 틀린 KV 를 서빙하기 때문이다.

## 11. 철회한 주장 (반복 방지) — 2026-08-31 이전

이후 세션의 철회 목록은 §12.11 에, 2.8 관련은 §13.4~§13.6 에 있다.
같은 실수가 반복됐다 — **간헐적 실패를 소수 시행 또는 검증되지 않은 계측으로 판단**.

| 철회한 주장 | 실제 |
|---|---|
| DRAM 상한 136.6 GB/s | 401.7 (단순 루프로 측정한 하한) |
| "GPU 4장이면 DRAM 포화 → GDS 필연" | per-GPU 와 집계 대역폭 혼동 |
| 256 KiB 에서 GDS 이득 | 서로 다른 두 임계 사이의 틈, 64·128 KiB 에선 GDS 가 더 느림 |
| `put_time` 0.13 ms → "스토리지가 700배 빠름" | 비동기 제출이라 실제 쓰기 미포함 |
| 4 MiB 정렬이 손상 원인 | 정렬해도 재현 |
| 클라이언트/서버 버전 불일치가 원인 | 별개 문제였고 주원인 아님 |
| UCX CUDA 플러그인 누락이 원인 | 무관 |
| store alias 가 원인 (주장→철회→재주장→철회) | 유효 계측에서 효과 없음 |
| "잔여는 read 측" | store 완료 미확인 상태의 분류였음 |
| 실패율 10% / 85% / 100% / 65% / 75% | 계측 결함(프롬프트 중복→기준 패스가 캐시 히트, 컨테이너 미초기화, 침습적 관측) |
| "loop 모드가 감도 낮아 80/80 통과" | loop 도 실패 |
| "정확히 4 MiB chunk 하나가 chunk 경계에서" | 오프셋·크기 모두 가변 |

**다음 세션 규칙:** 인과를 주장하기 전에 (a) 계측 전제를 코드로 강제하고, (b) 신뢰구간을
붙이고, (c) 한 번에 변수 하나만 바꾼다.

---

# 12. 2026-09-01 세션 — DAOS 로 확정, 서명 확보

이 세션의 질문은 "정합성 문제가 DAOS 문제인지"였고, **답은 그렇다**. §3 의 마지막 미배제
변수를 측정으로 닫았고, 손상의 정확한 형태를 얻었다. 재현기는 C 로 이식했다
(`tests/dfs_integrity.c`, 보조 `tests/dfs_integrity_ab.sh`·`tests/agg_ab.sh`).

client-6 은 다른 사용자의 CXL 작업 때문에 사용하지 않았다. 전부 **client-5** 에서 했다.

## 12.1 완전 스톡 클라이언트도 같은 비율로 실패 (핵심)

cell1 `/var/daosbuild/daos-stock`(스톡 `841487de8`)를 **`--build-deps=yes` 로 prereq 까지
새로 빌드**(mercury 는 0006 rkey 패치 없음, UCX 는 `--without-cuda --without-gdrcopy`),
prefix `/var/daos-stockfull`. RPATH 보존을 위해 client-5 에도 **같은 경로**로 rsync.

교차(interleaved) A/B, 28 MiB · 16 스레드 · burst, 컨테이너 `crp2g4`:

| arm | 결과 |
|---|---|
| 패치 클라(`/opt/daos-gds-gpu`) | 25/3840 = **0.651 %** (95 % CI 0.441–0.959) |
| **완전 스톡 클라(`/var/daos-stockfull`)** | 34/3840 = **0.885 %** (95 % CI 0.634–1.235) |

구간이 겹치고 스톡이 오히려 높다. 두 arm 을 **블록이 아니라 교차**로 돌린 것이 중요하다 —
실패율은 서버 상태에 따라 시간당 편차가 크고, §11 이 기록한 실패가 전부 그 편차를 arm 차이로
읽은 것이었다. 검증: 스톡 `libcart.so.4` 에 `HG_Bulk_import_rkey` 심볼 0개, mercury 트리에
0006 패치 미적용, `ucx_info -b` 에 CUDA/gdrcopy 매크로 없음.

⇒ **우리 패치·커넥터·LMCache·GPU-direct prereq 전부 무죄. DAOS 자체의 결함이다.**

## 12.2 손상의 정확한 서명 (태그된 페이로드로 확보)

이전 패턴 `base[i] = (i*31 + tid*101) & 0xff` 은 **다른 스레드의 데이터와 같은 스레드의
오프셋 이동 데이터가 수학적으로 구별 불가**였다(둘 다 바이트값을 상수만큼 이동). 그래서
"own pattern shifted by 245" 같은 판독이 나왔다 — 실제로는 다른 객체의 데이터였을 수 있다.
지금은 8바이트 워드마다 `(tid<<56)|(round<<48)|payload_offset` 을 심어 **모든 바이트가
자기 출처를 말한다**.

관측된 실패의 압도적 다수는 하나의 형태다:

> **읽기 버퍼의 정확히 DFS chunk 하나(4 MiB = 524288 워드) 구간이, 같은 오프셋·같은 라운드의
> _다른 객체_ 데이터로 채워져 돌아온다.** 나머지 구간은 전부 정확하다.

- 구간 크기는 **컨테이너 chunk 크기를 따라간다**: chunk 1 MiB 컨테이너(`ci_1m`)에서는 손상
  구간도 ~1 MiB(131 072 워드). ⇒ "chunk 하나가 통째로 잘못 배달된다"가 정확한 표현이다.
- 드물게 **zeros**(구멍) 또는 **stale round**(같은 객체의 이전 라운드 데이터)도 나온다.
- 시작 오프셋은 대개 파일 오프셋 기준 4 MiB 경계(payload_off 36 이므로 워드 4194264 부터).
- **at-rest 는 대체로 정상**: 같은 런에서 스레드 종료 후 단일 스레드로 다시 읽으면 깨끗하다.
  ⇒ 저장된 바이트는 맞고, **읽기가 저장되지 않은 바이트를 돌려준다.**
- 부하 중 즉시 재읽기: A/B 전체에서 clean 25 / STILL WRONG 34 — **틀린 답이 남을 수도 있다.**

## 12.3 방아쇠는 "쓰기와 읽기가 섞일 때"

한 번에 하나만 바꾼 결과(모두 `tests/dfs_integrity.c` 플래그):

| 구성 | 결과 |
|---|---|
| 조용한 단일 스레드 쓰기 + 조용한 검증 3회 (`-W -Q -V 3`) | 깨끗 (48/48) |
| 조용히 쓴 뒤 **읽기만** 16 스레드 (`-W -M`) | **0/4320** (18 GB/s) |
| **쓰기만** 16 스레드 + 조용한 검증 (`-N -V 3`) | 깨끗 |
| 단일 스레드 쓰기+읽기 150 라운드 (`-t 1`) | 0/150 |
| 16 스레드 쓰기+읽기 (기본) | **0.2–1.9 %** |
| 16 스레드, 쓰기와 읽기 사이 2초 대기 (`-d 2000`) | 0/240 (표본 부족, 참고만) |

⇒ 읽기 동시성만으로는 안 나오고, 쓰기 동시성만으로도 안 나온다. **둘이 섞여야** 나온다.

## 12.4 gate 가 아닌 것

- **복제·oclass 아님**: `SX`(rd_fac 0, 복제 없음) 0.21–0.78 %, `RP_2G1`·`RP_2G4`·`RP_2G8`
  전부 재현. 처음 `S1`(단일 shard)이 0/2560 으로 깨끗해 "shard fan-out 이 조건"이라 봤으나
  표본을 1920 으로 올리자 **S1 도 2/1920 실패** → **그 판독은 철회한다.** 같은 배치에서
  S4·SX 가 0/1920 이었다 — 0.2~0.9 % 대에서 2000 표본은 arm 을 가르지 못한다(§6, §11).
- **컨테이너 신선도 아님**: 갓 만든 `ci_plain` 도 6/1920.

## 12.5 서버 쪽 증거 — DAOS 가 스스로 손상을 검출한다

**양 rank** 의 엔진 로그(`/var/log/daos/daos_engine.0.log`, cell1·cell2):

```
csum src/vos/vos_csum_recalc.c:111 csum_agg_verify() calc ({... first_csum: 0 ...})
                                                  != phy ({... first_csum: 2092910456 ...})
vos src/vos/vos_aggregate.c:1230 fill_one_segment() CSUM verify error: DER_CSUM(-2021)
vos src/vos/vos_aggregate.c:1817 flush_merge_window() Fill segments 0-3fffff error: DER_CSUM
RAS EVENT id: [device_media_error] msg: [Device: ba123b03 csum error logged from tgt_id:6]
```

- 실패하는 창은 **정확히 `0-3fffff` = 4 MiB**, 즉 클라이언트가 보는 손상 구간과 같은 크기다.
- `dmg storage query list-devices --health`: 모든 NVMe 가 **Media/Read/Write Errors 0**,
  그러나 장치마다 **Checksum Errors 2–6**. ⇒ 하드웨어 미디어 오류가 아니라 **DAOS 내부**
  검사 실패다.
- 이 DER_CSUM 은 **checksum 을 켠 컨테이너(`ci_csum`)를 쓰기 시작한 시각부터** 나타난다.
  즉 원래도 손상되고 있었고, checksum 이 없을 때는 **아무도 검출하지 않고 클라이언트로
  배달**된 것이다.

## 12.6 checksum 은 해결책이 아니라 검출기

`cksum:crc32,srv_cksum:on` 컨테이너에서 **조용한 손상은 관측되지 않았다**(약 4 000 읽기).
대신 일부 런이 **EIO 로 중단**됐다(스레드 rc=5). 즉 침묵이 오류로 바뀐다 — KV 백엔드에
당장 쓸 수 있는 **탐지** 수단이지만 정합성 보장은 아니다.

## 12.7 부수 증상 2건 (같은 부하에서)

1. **지속 부하에서 쓰기가 실패한다**: `dfs_write` → `daos_array_write()` →
   `DER_MISC(-1025)` → 앱에는 EIO. 16 스레드·28 MiB 를 몇 분 돌리면 나오고, **한가해지면
   회복**된다(가벼운 부하는 정상). 이 때문에 후반 측정의 표본이 잘렸다 — 런당 완료 읽기 수를
   반드시 요약줄에서 되읽을 것.
2. **컨테이너 close 마다** `dtx_flush_on_close() Some DTX in CoS cannot be committed` +
   `Fail to flush CoS cache: rc = -1025`(양 rank). 소스(`src/dtx/dtx_common.c:1559`)를 보면
   회계 조건에서 루프를 끊고 비동기 배치 커밋으로 넘기는 경로라 즉시 데이터 손상 경로는
   아니지만, 상류 제출 시 함께 붙일 것.

## 12.8 VOS aggregation 가설 — **검정력 있게 재시험한 결과 기각**

`tests/agg_ab.sh` 로 `reclaim:lazy`(aggregation ON) 대 `reclaim:disabled`(OFF) 를 블록 교대로
돌렸다. 런당 160 읽기, 블록당 8 런, arm 당 4 블록 = arm 당 5120 읽기. 짧은 런 + 런 사이
대기로 §12.7-1 의 EIO 절단을 없앴다(1차 실험 truncated run 0건). **양성 대조**: OFF 블록마다
NVMe free 가 701→626 GB 로 줄고 ON 블록에서 회복 → 속성이 실제로 적용됐음을 확인.

| 실험 | ON | OFF |
|---|---|---|
| 1차 (블록마다 ON 먼저) | 77/5120 = **1.50 %** | 27/5120 = **0.53 %** |
| 2차 (블록마다 OFF 먼저, 후반 환경 열화로 부분) | 54/3758 = **1.44 %** | 51/3840 = **1.33 %** |

1차만 보면 3배 차이에 Wilson 구간도 안 겹쳐 확정처럼 보인다. **틀렸다.** 순서를 뒤집은 2차에서
차이가 사라졌고, 블록별로 쪼개 보면 이유가 분명하다:

| | 1차 ON | 1차 OFF | | 2차 OFF | 2차 ON |
|---|---|---|---|---|---|
| block 1 | 48/1280 (3.75 %) | 11/1280 (0.86 %) | block 1 | 40/1280 (3.13 %) | 6/1280 (0.47 %) |
| block 2 | 7/1280 (0.55 %) | 8/1280 (0.63 %) | block 2 | 9/1280 (0.70 %) | 44/1280 (3.44 %) |
| block 3 | 8/1280 (0.63 %) | 3/1280 (0.23 %) | block 3 | 2/1280 (0.16 %) | 4/1198 (0.33 %) |
| block 4 | 14/1280 (1.09 %) | 5/1280 (0.39 %) | | | |

**실패는 버스트로 온다.** 같은 arm 안에서도 블록에 따라 0.16 %–3.75 % 로 20배 흔들리고,
양쪽 실험의 총합은 각각 딱 한 블록(48, 44, 40)이 지배한다. 즉 **읽기를 독립 베르누이 시행으로
보고 계산한 Wilson 구간은 이 데이터에서 무효**다(과산포). 1차의 "겹치지 않는 구간"은 그
착시였다. 블록을 단위로 보면 ON 이 높은 블록 5, OFF 가 높은 블록 2 — 부호검정 p≈0.45.

⇒ **aggregation 은 원인이 아니다.** aggregation 을 끈 상태에서도 78/8960 = 0.87 % 로 손상이
계속된다. 앞선 세션의 `0/737` 은 검정력 부족이었다(그 때 p≈0.008 이라고 적어둔 그 확률).

**측정 규칙(다음 세션 필수):** arm 비교는 **런 단위 교대**로 하고(§12.1 의 패치/스톡 A/B 처럼),
블록 단위로 묶어야 한다면 순서를 뒤집은 대조도 함께 돌릴 것. 풀 읽기수 기준 이항 신뢰구간만
믿고 arm 차이를 주장하지 말 것.

## 12.8b 동시성 없이도 재현된다 — 조건은 "덮어쓰기 세대"

aggregation A/B 뒤 환경이 열화된 상태에서 조용한 대조군을 다시 돌리다 발견했다. **스레드 없음,
동시 접근 없음**: 16개 객체를 단일 스레드로 쓰고, 같은 프로세스가 단일 스레드로 되읽는다.

- 갓 만든 컨테이너에 1세대만 쓰면 깨끗(0/16 × 3 패스).
- **같은 객체를 반복해서 덮어쓰면** 6세대째부터 틀리기 시작해 20세대 중 8세대에서 1–2개 객체가
  틀렸다(9/320 ≈ 2.8 %).
- 쓰기와 읽기를 **별개 프로세스**로 나눠도 동일(클라이언트 핸들/캐시 배제). 12세대에서
  객체당 0–13개가 틀렸다.
- 쓰기 프로세스 종료 후 **10초 대기해도** 줄지 않는다(0초 31/96, 10초 22/96) → 단순한 커밋
  가시성 지연이 아니다.
- 세대마다 다른 round 태그(`-R`)를 쓰면 틀린 조각의 정체가 나온다: **이전 세대의 데이터**
  또는 **다른 객체의 데이터**. 같은 객체를 연속으로 읽으면 틀린 조각이 **패스마다 바뀐다**
  (t7: 12 MiB 에 t13 데이터 → 같은 결과 → 다음 패스엔 16 MiB 에 t15 데이터) — 디스크 내용이
  틀린 게 아니라 **읽기가 매번 다른 조각을 잘못 가져온다**.

⚠️ **단, 이 관측은 풀이 이미 몇 시간 부하를 받아 열화된 뒤에 얻은 것이다**(그 시점엔 쓰기가
간헐적으로 EIO). 건강한 풀에서 처음부터 재현되는지는 **아직 확인 안 했고, 다음 세션 1순위**다.
성립하면 상류 재현기가 "16 스레드 burst" 에서 **"단일 스레드로 몇 번 덮어쓰고 읽기"** 로
극적으로 단순해진다.

## 12.8c 서버가 bulk 핸들 역직렬화에 실패한다

부하가 쌓이면 cell2 엔진이 `tgt_update` RPC 를 못 푼다(오늘 7423건, 03:22 부터):

```
mercury->bulk [error] mercury_bulk.c:1431 hg_bulk_deserialize() Could not deserialize address
mercury->bulk [error] mercury_bulk.c:2783 HG_Bulk_deserialize() Could not deserialize handle
hg src/cart/crt_hg.c:1315 crt_rpc_handler_common() _unpack_body failed, opc: 0x40a000b: DER_HG
```

클라이언트에는 §12.7-1 의 `DER_MISC` → EIO 로 보인다. **RPC 본문 안의 bulk 핸들이 깨져서
도착한다**는 뜻이므로, 페이로드만이 아니라 **RPC 본문도 손상된다**는 해석이 가능하다 —
"chunk 하나가 다른 객체 것으로 바뀐다"와 같은 뿌리일 수 있다.

wire 포맷 차이 때문일 가능성은 배제했다: 패치 mercury 와 스톡 mercury 의 `src/` 차이는
`HG_Bulk_import_rkey` 스텁(호출자 없음)·그 헤더·`na_ucx.c` 의 TLS 한 줄(우리는
`NA_UCX_EXTRA_TLS=` 로 무력화)뿐이고 **직렬화 코드는 동일**하다.

## 12.8d 아직 바꿔보지 않은 변수 하나 — **서버 빌드**

§12.1 이 배제한 것은 **클라이언트** 패치다. 모든 arm 이 **같은 패치 서버**(`/opt/daos-gds`)를
공유했다. 서버까지 완전 스톡(`/var/daos-stockfull`)으로 바꾸려면 `daos_server` 재시작이
필요하고, §8-1 대로 SPDK wedge → format → **풀 파기** 위험이 있다. 사용자 판단이 필요한
파괴적 작업이므로 이 세션에서는 하지 않았다. 상류 제출 시 "서버는 2.9.100 백포트 빌드"라고
명시할 것.

## 12.9 환경 (다음 세션이 이어받을 상태)

- cell1: `/var/daos-stockfull`(완전 스톡 2.9.100, 빌드 트리 `/var/daosbuild/build-stockfull`,
  로그 `/var/daosbuild/build-stockfull.log`, 스크립트 `/var/daosbuild/build_stockfull.sh`).
  기존 `/var/daosbuild/build-gpu` 와 별도라 패치 빌드는 그대로 살아 있다.
- client-5: `/var/daos-stockfull`(같은 경로 필수), `/root/dfs_integrity.c`,
  `/root/dfs_integrity_{patched,stock}`, `/root/dfs_integrity_ab.sh`. cell1: `/root/agg_ab.sh`.
- 새 컨테이너(gdspool): `ci_plain`(RP_2G4 4 MiB) `ci_csum`(+crc32) `ci_1m`(1 MiB chunk)
  `ci_s1` `ci_s2` `ci_s4` `ci_sx`. 풀 `reclaim` 은 **lazy 로 복원**해 두었다.
- 서버는 건드리지 않았다(패치 빌드 그대로 실행 중, 재시작 없음 — §8-1 준수).

## 12.10 다음 작업 순서 (개정 2)

1. ~~aggregation A/B~~ — **완료, 기각(§12.8).**
2. **§12.8b 를 건강한 풀에서 재현**하라. 풀을 쉬게 하고(또는 `ci_*` 테스트 컨테이너를 지워
   공간·메타데이터를 회수하고), 갓 만든 컨테이너에서 단일 스레드 덮어쓰기 세대를 1→30 까지
   올리며 세대별 실패율을 기록할 것. 성립하면 재현기가 단일 스레드로 단순해진다.
3. `ci_1m` 외에 chunk 512 KiB·16 MiB 로 손상 구간 크기 = chunk 크기를 한 번 더 확인.
3. 손상 발생 시각의 엔진 로그를 `DD_SUBSYS=vos,bio,object` 로 좁혀 확보(런타임
   `dmg server set-logmasks` 로 가능, 재시작 불필요).
4. **상류 제출**: §12.1 A/B 표, §12.2 서명(태그 페이로드 출력 원문), §12.5 서버 로그 + 장치
   카운터, §12.3 방아쇠 표, §12.6 checksum 거동, §12.7 부수 증상 2건, 재현기 C 파일.
5. Hub 정정 공지(`719497ff-…`) 갱신 — "원인 미해결" → "DAOS 확정, aggregation 의심".
6. **그때까지 이 백엔드는 사용 불가로 유지한다.**

## 12.11 이 세션에서 철회/정정한 것

| 주장 | 실제 |
|---|---|
| "S1(단일 shard)은 면역" | 표본 늘리자 2/1920 실패. 0/2560 은 검정력 부족이었다 |
| "checksum 켜면 손상이 사라진다" | 조용한 손상은 사라지지만 EIO 로 나온다. 검출기이지 수정이 아니다 |
| "at-rest 도 손상된다"(초판 판독) | 두 계측 결함이었다 — (a) 태그 형식이 바뀐 객체를 옛 형식으로 검증, (b) EIO 로 쓰기가 중단된 객체는 라운드가 섞인 게 정상 |
| "쓰기 동시성만으로 at-rest 가 깨진다" | `-N -V 3` 깨끗 |
| "aggregation 이 원인일 수 있다"(§12.8 초판, 0/737) | 검정력 있게 재시험하니 기각 — 실패가 버스트로 오고 순서를 뒤집으면 차이가 사라진다 |
| "쓰기+읽기 혼합이 필요조건"(§12.3) | 덮어쓰기 세대를 쌓으면 **단일 스레드로도** 재현(§12.8b, 단 열화된 풀에서 관측) |

---

# 13. "2.8.0-rc3" 서버에서도 동일하다 (2026-09-01, 서버 reformat 실측)

> **⚠️ 정정(2026-09-01 심야, 사용자 지적):** 이 절에서 "2.8.0-rc3" 라고 부른 서버는 upstream
> 이 아니라 **`port/2.8-wsd` = upstream rc3 + 66 커밋**(zfs-cap/WS-D staging, bio/vos 수정 포함)
> 이다. TAG 파일이 2.8.0-rc3 인 것을 upstream 으로 오기했다. 실행 바이너리 검증: 8/23 빌드
> RPM 의 `libbio.so` 에 WS-D 문자열 존재. 이 절이 실제로 증명한 것은 "**rc3 기반 ExaStor
> 브랜치**도 동일 손상"이며, **완전 upstream 재현은 §18 이 확정**했다.

질문: "2.8-rc3 에서도 동일할까?" — **동일하다.** 추론이 아니라 같은 하드웨어·토폴로지·provider
에서 서버를 2.8.0-rc3 로 되돌려 측정했다(사용자 승인 후 gdspool 파기).

## 13.1 왜 2.8-rc3 인지, 무엇이 같은 코드인지

`port/2.8-wsd` 와 `verify/2.8-rc3-zoneinstr` 둘 다 `TAG=2.8.0-rc3`(상류 `3604d406ef`).
2.8.0-rc3 ↔ 2.9.100(`841487de8`) diff:

| 경로 | 차이 |
|---|---|
| `src/client/array/dc_array.c` — DFS 읽기를 chunk 로 쪼개는 곳 | **동일** |
| `src/vos/vos_aggregate.c` | **동일** |
| `src/vos/vos_csum_recalc.c` — DER_CSUM 을 찍는 함수 | **동일** |
| `src/bio/` 전체 | **동일** |
| `src/object/srv_obj.c` | 24+/74− |
| `src/vos/vos_io.c` | 36+/14− |
| `src/cart/` (crt_hg·crt_bulk 등) | 1128+/1079− |

즉 손상 신호를 만드는 코드는 두 버전이 같고, 크게 바뀐 건 전송 계층뿐이다.

## 13.2 전환 절차 (그대로 재현 가능)

양 cell 에 **2.8.0-rc3 RPM 이 이미 설치돼 있었다**(`daos-server-2.8.0-4.el8`, `/usr/bin`).
`/opt/daos`(소스빌드 prefix)와 RPM 은 **build-id 동일**(libdaos `8e52765f`, libdfs `54290720`)
— stripped 여부만 다르므로 클라(prefix)와 서버(RPM)가 같은 빌드다. §8-3 의 불일치 함정 회피.

1. 설정 백업: `/root/daos-cfg-backup-2.9/`(양 cell), `/root/daos-agent-unit-2.9.bak`(client-5)
2. client-5 agent 정지 → 양 cell `systemctl stop daos_server`
3. drop-in 을 `/usr/bin/daos_server` 로 교체 + `daemon-reload`
4. 2.9 메타데이터 제거: `/var/daos/control_meta/daos_control/control_raft`, `/mnt/daos0/*`,
   root shmem `ipcrm` → **여기서 풀이 파기된다**
5. 기동 → `dmg -i storage format`(cell1) + **`dmg -i -l 10.100.230.82 storage format`**(cell2 는
   명시 필요) → 양 rank Joined
6. `dmg -i pool create gdspool --scm-size=8G --nvme-size=200G` (416 GB)
7. client-5: cell1 `/opt/daos` 를 **같은 경로로** rsync, `libna_plugin_ucx.so`→`/usr/lib64/mercury`,
   `/lib64/ucx` 심볼릭, agent unit 을 `/opt/daos` 로 sed 후 재시작
8. 재현기 재빌드: `gcc ... -I/opt/daos/include -L/opt/daos/lib64 ... -o dfs_integrity_28`

**SPDK wedge 는 일어나지 않았다** — format 이 한 번에 통과했다(§8-1 은 재시작 일반론이고, 이번
전환에서는 문제 없었다).

## 13.3 실측 (2.8.0-rc3, 갓 포맷한 풀, provider ucx+rc_v, RP_2G4/4 MiB)

**정상상태에서 2.9.100 과 같은 범위다:**

| arm | 2.8.0-rc3 | 2.9.100 (동일 프로토콜) |
|---|---|---|
| mixed 쓰기+읽기 16 스레드 (16×40 ×8) | **68/5120 = 1.33 %** | 34/5120 스톡=0.885 %, 25/5120 패치=0.651 % |
| mixed, payload 오프셋 0(정렬) — 교대 A/B | 19/5120 = 0.37 % | (미측정) |
| mixed, payload 오프셋 36(straddling) — 교대 A/B | 35/5120 = 0.68 % | 0.65–0.89 % |
| read-only 동시성(조용히 쓴 뒤 읽기만) — 안정화 후 | 0/3840 | 0/4320 |
| 단일 스레드·단일 객체 mixed | 0/100 | 0/150 |

서명도 같다: 4 MiB 한 chunk 가 **같은 오프셋의 다른 객체 데이터**, 또는 같은 객체의 다른
오프셋 데이터(`own data from offset N (-20971520)`), 드물게 stale round.

⇒ **결함은 2.9 에서 새로 생긴 것도, GPU-direct 백포트가 만든 것도 아니다.** 2.8/2.9 공용
코드(§13.1)에 있다.

## 13.4 포맷 직후 과도 구간 — 훨씬 심하다 (기록용, 재현 실패)

포맷 후 첫 1시간 동안은 비율이 자릿수로 달랐다:

- 첫 smoke test(4 스레드×3): **8/12 = 67 %**
- 조용한 대조군(동시성 전무, 16 객체 단일 스레드 쓰기→읽기): **10~13/16 객체 오류** — 2.9 에서는
  이 arm 이 항상 깨끗했다
- read-only 동시성 8회 연속: **275 → 181 → 152 → 133 → 96 → 57 → 29 → 0 /640** (단조 감소)

이후 같은 arm 들이 재현되지 않았다(read-only 0/3840, 조용한 대조군 clean 3회). 덮어쓰기를
반복하면 "치유"되는 초기 상태 의존 현상으로 보이며, **mixed load 로 다시 유도되지 않았다**
(cycle 3회: read-only before/after 모두 0/640). 원인 미규명 — 상류에 붙일 만한 관측이지만
현재 상태로는 주장하지 말 것.

## 13.5 정렬(alignment)은 완화책이 아니다

한 번의 측정에서 straddling 25.5 % 대 aligned 5.5 % 가 나와 완화책처럼 보였으나, **교대 A/B
5120 읽기씩**으로 다시 재면 **0.68 % 대 0.37 %** 로 줄어든다(런별로 13:0, 0:11, 2:15 처럼
뒤집힘). 약한 경향은 있으나 버스트를 감안하면 결정적이지 않다 → **§3 의 "정렬 무관" 판정 유지.**
KV 커넥터 페이로드를 chunk 정렬해도 해결되지 않는다.

## 13.6 과거 "2.8 + UCX 무결 30/30" 판정에 대하여

그 판정(Hub v4)은 `tests/test_manyread.py` — 30개 객체를 각각 **한 바이트를 반복한 값**으로
채워 순차 읽기·md5 비교하는 테스트였다. 두 가지 이유로 결함 부재의 근거가 못 된다:

1. **검정력**: 이번에 측정된 정상상태 비율(0.4~1.3 %/읽기)이면 30 읽기가 전부 통과할 확률이
   67~89 %, 3회 반복 전부 통과도 30~70 % 다.
2. **상수 채움의 맹점**: 이번 2.8 실측 실패 중에는 `own data from offset N` (같은 객체의 다른
   오프셋 조각)이 섞여 있다. 객체 전체가 같은 바이트면 **그 유형은 원리적으로 검출 불가**다.

**교훈: 무결성 검증 페이로드는 위치·객체를 식별하는 태그여야 한다**(`tests/dfs_integrity.c`의
8바이트 태그). 상수/난수 한 덩어리로는 이 결함의 일부가 보이지 않는다.

## 13.7 현재 환경 상태

- cell1/cell2: **DAOS 2.8.0-rc3**(`/usr/bin`, RPM) 실행 중, 양 rank Joined, pool `gdspool`
  416 GB(SCM 8 G/rank + NVMe 200 G/rank), 컨테이너 `ci_plain ci_quiet ci_a0 ci_a36 ci_m28`
- client-5: `/opt/daos`(2.8 클라), agent unit 도 2.8, 재현기 `/root/dfs_integrity_28`
- 2.9 설치본은 **그대로 보존**: `/opt/daos-gds`(서버), `/opt/daos-gds-gpu`, `/var/daos-stockfull`
- **2.9.100 으로 되돌리려면**: drop-in 을 `/opt/daos-gds/bin/daos_server` 로 복원(백업 있음) →
  §13.2 의 4~6 단계 반복(= 2.8 풀 파기, 테스트 데이터뿐) → client-5 agent unit 복원

---

# 14. 상류 선행 보고 조사 (2026-09-01)

## 14.1 어디를 봐야 하는가

**`daos-stack/daos` 는 GitHub Issues 가 비활성**(`has_issues: false`)이다. GitHub 에 있는 건
전부 PR 이고, 버그 보고는 **JIRA `daosio.atlassian.net`** 에 있다. 이 JIRA 는 **익명 읽기 가능**:

```
# 검색 (v2 /search 는 410 Gone, v3 를 쓸 것)
https://daosio.atlassian.net/rest/api/3/search/jql?jql=project%3DDAOS%20AND%20summary~%22corruption%22&fields=key,summary,status,created
# 개별 티켓
https://daosio.atlassian.net/rest/api/2/issue/DAOS-18862?fields=summary,status,resolution,versions,description
```

## 14.2 결론 — 우리 서명과 일치하는 보고는 없다

검색한 축: `summary~corruption`(40건), 2026년 이후 Bug+"data corruption"(28건),
`"wrong data"/"stale data"/"incorrect data"`, `"another object"`, UCX·rcache·corruption,
그리고 우리 로그 문자열 그대로(`csum_agg_verify`, `hg_bulk_deserialize`/`deserialize address`,
`CoS cache`). 손상 티켓은 전부 **rebuild/reintegration/exclusion**, **EC aggregation**,
**메모리 손상(double-linked list)**, 또는 테스트 하네스 문제였다. **장애·리빌드 없이 평상시
읽기가 다른 객체의 chunk 를 조용히 돌려준다**는 보고는 없다.

## 14.3 가장 가까운 이웃 — DAOS-18862 (Cannot Reproduce 로 종결)

> **DAOS-18862** "release/2.8: Checksum mismatch at index 42/64" — 2026-04-21, affects 2.7,
> **Resolved / Cannot Reproduce**, 원인 미규명.
> 환경: **MD-on-SSD**, **UCX(dc_x)**, `release/2.8`(c22a7958), 2 TB 풀, HDF5 exerciser.
> 클라이언트 읽기에서 `Checksum mismatch at index 42/64 59887 != 27441`.

우리와 같은 축이 셋(MD-on-SSD · UCX · 2.8 계열 · 읽기 경로 체크섬 불일치)이고, 다른 축이 둘
(EC_8P3GX vs 우리 RP_2G4, 체크섬 ON 이라 침묵이 아니라 오류로 표면화). **재현 불가로 닫혔는데
우리는 재현기가 있다** → 상류 제출 시 이 티켓을 반드시 참조/재오픈 후보로 연결할 것.
곁가지 힌트: 그 환경은 **`UCX_ENABLE_RCACHE` 를 서버·클라 양쪽에서 끈** 상태였다 — 보고자도
UCX 등록 캐시를 의심했다는 뜻이다(우리는 아직 이 노브를 시험하지 않았다).

## 14.4 열려 있는 신규 티켓 — DAOS-19569 (확인 필요)

> **DAOS-19569** "IOM process did not correctly handle multiple IODs case" — 2026-08-31 생성,
> affects **2.8 · 3.0 Community**, **Awaiting backport**, component **Erasure Code**.
> 본문: "Some IOM detailed handling did not correctly handle multiple IODs case" +
> **"This possibly cause data corruption in special cases."**

28 MiB DFS 읽기는 dc_array 가 chunk 마다 IOD 로 쪼개므로 **정확히 multi-IOD 케이스**다. 단
component 가 EC 이고 우리는 RP 이므로 경로가 EC 전용인지 확인해야 한다. 패치는 우리 로컬
`origin/master`(2026-08-28 fetch)보다 최신이라 트리에 없다 → **fetch 후 diff 를 보고, 우리
재현기로 전후 비교할 것. 다음 세션 1순위 후보.**

## 14.5 참고로 관련되지만 조건이 다른 것들

| 티켓 | 상태 | 왜 우리 것이 아닌가 |
|---|---|---|
| DAOS-18368 "Data corruption ... MDonSSD" | Resolved (2.6.5/2.8 수정) | reintegration 이 방아쇠, 우리는 리빌드 없음 |
| DAOS-18524 "DER_CSUM -2021 + Data corruption found for recx" | Resolved | reintegration 중 |
| DAOS-18869 "data corruption after two ranks failed (spdk)" | Open | rank 장애 필요 |
| DAOS-16970 "Timeout and read corruption on target exclusion" | Open | target exclusion 필요 |
| DAOS-3841 "fetch returning data at wrong offset" | Resolved (2019) | 우리 `own data from offset N` 유형과 결이 같으나 시기가 다름 |
| DAOS-19451 / PR #18885 "enable checksum by default on non-v0 pools" | Open PR | 상류도 침묵 손상 위험을 의식해 기본값 전환 중(§12.6 과 맞물림) |
| DAOS-17321 / PR #18940·#18942 ddb `csum_check` | Open PR | 오프라인 체크섬 검증 도구 추가 중 |

## 14.6 우리 빌드에 없는 상류 수정 (로컬 `origin/master` 2026-08-28 기준)

| 커밋 | 2.8-rc3 | 2.9.100 | 우리 증상과의 관계 |
|---|---|---|---|
| `1ff454966b` DAOS-19537 array: fix set_size at chunk boundaries | ✗ | ✗ | truncate/shrink 경로 — 우리는 축소를 안 하므로 무관 |
| `4f919653da` DAOS-15847 object: restore iov_len for fetch on dup-only SGLs | ✗ | ✓ | fetch SGL 의 iov_len 만 어긋나는 버그(데이터 위치는 정상) |
| `205e513c25` DAOS-18901 vos: Cap merged extent size | ✗ | ✓ | aggregation 병합 크기 제한 — 양쪽 다 손상되므로 결정적이지 않음 |
| `c61ae699bb` DAOS-19036 dtx: handle DTX race issues | ✗ | ? | §12.7-2 의 DTX CoS 증상과 맞춰볼 가치 있음 |

즉 **2.8-rc3 는 2.9.100 이 가진 fetch/aggregation 수정 두 건을 아직 안 갖고 있는데도 손상
비율이 같은 자릿수**(§13.3)다. 이 조합은 "그 두 수정이 원인이 아니다"는 쪽 근거다.

---

# 15. 원인 분기 트리 완주 — transport 무관, mercury core/서버 fetch 경로로 수렴 (2026-09-01 야간)

외부 검토 계획(/tmp/daos-fix.md)의 Phase 1~2 + P1 transport arm 을 실행했다. **계측 빌드 없이
가능한 배제는 전부 끝났고, 남은 용의자는 mercury core bulk 로직과 DAOS 서버 fetch/bulk 버퍼
수명 둘뿐이다.**

## 15.1 Phase 1 — 런타임 검증 (전부 통과)

- 세 노드(cell1·cell2 엔진, client-5)가 **실제 로딩하는** `libna_plugin_ucx.so` 에
  `ucp_ep_flush_nbx` 심볼 존재 = **DAOS-18862 의 Mercury put-flush 수정이 이미 들어 있다.**
  ⇒ 상류 제출 프레임: **"fix present, reproducer still fails."**
- UCX 1.20.0 확인. `0005_ucx_put_flush.patch` 는 2.8/2.9 트리 동일 해시.
- rcache 환경변수 정정 확인: 소스(`crt_init.c:579`)는 `UCX_RCACHE_ENABLE=n` 을 설정.
  `CRT_MRC_ENABLE=0` → `FI_MR_CACHE_MAX_COUNT=0` + `UCX_RCACHE_ENABLE=n` + 로그
  `Disabling MR CACHE`. **2.8 은 클라이언트 MRC 기본 ON**(c22a79582a 정책, 2.9 트리엔 없음).

## 15.2 Phase 2 — 교차 A/B 3종 (전부 "원인 아님")

하네스: `tests/crossover_ab.sh`(AB BA BA AB 균형 순서, stderr 보존, **런 단위 부호반전
순열검정** — 기존 dfs_integrity_ab.sh 의 결함 3종 수정판), `tests/procmatrix_ab.sh`.
재현기에 `-T`(tid base)·`-A`/`DFSI_BUFMODE`(reuse|malloc|mmap) 추가 — **`-T` 없인 멀티프로세스
arm 이 구조적으로 장님**(전 프로세스 tid 0 → 교차 치환이 정답으로 검증됨).

| arm | 결과 | 판정 |
|---|---|---|
| client MR cache off (`CRT_MRC_ENABLE=0`+`UCX_RCACHE_ENABLE=n`, 마커 로그 확인) | 46/5120 vs 기준 79/5120, p=0.25 | **원인 아님 — off 에서도 손상** |
| 버퍼 VA 재사용 (reuse vs 매 라운드 새 mmap·기존 매핑 유지) | 59/4659 vs 64/4513, p=0.98 | **원인 아님** |
| 1×16 threads vs 16×1 processes | T 60/3666 vs P 16/4007, p=0.44 | **원인 아님 — 프로세스 분리로도 발생** |

## 15.3 서버 BIO bulk-handle cache — 원인 아님

`DAOS_IO_BYPASS=srv_bulk_cache` 를 양 엔진에 적용(로그 `debugging mode: srv_bulk_cache is
disabled` 양쪽 확인) 후 fresh pool 에서: **DFS 33/5120 손상 지속**(18/640·3/640·12/640),
raw obj 0/1920. cached bulk handle 은 무죄. 단 **DMA chunk 버퍼 풀 자체는 bypass 대상이
아니므로** bio DMA 버퍼 재사용은 아직 용의선상에 있다.

## 15.4 `ucx+tcp` — 측정 불가, 그 자체가 별도 결함

provider 를 `ucx+tcp` 로 바꾸면 **모든 update RPC 가 서버에서 결정적으로 실패**:
`hg_bulk_deserialize() Could not deserialize address` → DER_HG → 클라 DER_MISC. fresh pool
첫 RPC부터 100 %, 양 rank. 작은 RPC(pool 연결)는 정상 — **bulk 핸들(클라 워커 주소 내장)이
든 RPC 만** 깨진다. rc_v 에서 부하 시 간헐 발생하던 것(§12.8c)과 같은 실패가 tcp 주소
형식에선 항상 발생. **na_ucx 주소 직렬화 결함으로 상류에 별도 보고 가치.**

## 15.5 ★ `ofi+tcp` — RDMA 없이도 같은 서명으로 손상 (결정적)

libfabric NA 플러그인 + 커널 TCP(RDMA·MR·NIC DMA 전무)로 전환, fresh pool:

| | 결과 |
|---|---|
| DFS 16×40 ×8 | **83/5120 = 1.62 %** (10.78 %·0 ×5·0.94 %·1.25 % — 버스트 패턴 동일) |
| raw obj ×3 | **49/1920** (9·40·0) |
| 서명 | **동일**: 4 MiB chunk 하나가 같은 라운드·같은 오프셋의 다른 객체 데이터 |

⇒ **UCX·RDMA·NIC/PCIe DMA·MR 캐시 전부 최종 배제.** 서로 무관한 두 전송(ucx+rc_v RDMA,
ofi+tcp 소켓)이 같은 서명으로 손상 = 결함은 그 위 공통층이다.

## 15.6 분기 트리 최종 상태

```
client MRC off        → 손상 지속   (§15.2)
VA 재사용 제거        → 손상 지속   (§15.2)
프로세스 분리         → 손상 지속   (§15.2)
server bulk-hdl cache → 손상 지속   (§15.3)
raw obj API (dc_array/DFS 배제, nr=1 단일 recx) → 손상 지속 (아래)
ofi+tcp (UCX/RDMA 배제) → 손상 지속 (§15.5)
─────────────────────────────────────────────
남은 용의자:
  (a) mercury core bulk 로직 (mercury_bulk.c — 두 NA 플러그인 공용)
  (b) DAOS 서버 fetch 경로의 DMA/bulk 버퍼 수명
      (vos fetch 채움 ↔ bulk PUT 완료 사이 재사용; srv_bulk_cache 는 핸들만 캐시)
  (c) crt/object 층의 bulk 디스크립터-태스크 매칭
판별 수단 = Phase 3 경계 해싱 (S1/S2/S3/C1/C2/C3, 계측 빌드 필요)
```

**raw object 재현기**(`tests/obj_integrity.c`, 신규): daos_obj_fetch 직접 호출, dkey=chunk
번호, IOD 1개·recx 1개 — DFS·dc_array 완전 배제 상태에서 26/385·20/144 손상, 서명 동일.
계획서 §3.1 의 "DAOS-19569(EC IOM merge)는 우리 경로가 아니다"가 코드와 실측 양쪽으로 확정.

## 15.7 운영 발견 (재현·복구 조작법)

1. **§8-1 SPDK wedge 의 실제 원인 하나를 특정**: stale `/var/tmp/spdk_pci_lock_0000:*` +
   `/var/run/dpdk/spdk_pid*`. **rm 만으로 복구**되는 경우가 있다(dd·재포맷 불필요).
2. 그래도 blobstore 가 깨졌으면: setup.sh reset → **PCI 주소로 열거한 데이터 NVMe 만**
   `blkdiscard`(nvme24n1=OS 접근 금지 가드 포함) → SCM/raft 정리 → format.
3. **provider 전환 절차**: server.yml provider 수정 + 양쪽 재포맷 + **cell1 로컬 agent 와
   client agent 의 domain 을 provider 에 맞게**(ucx: `mlx5_0:1`, tcp 계열: netdev 명) + agent
   재시작. 서버 재기동 후 클라 agent 도 재시작(구 attach 정보로 DER_HG).
4. **client-5 firewalld 가 ofi+tcp bulk 를 막는다**: mercury tcp bulk 는 서버→클라 역방향
   연결. `firewall-cmd --zone=trusted --change-interface=ens255np0` (런타임, --permanent 아님).
5. cell1 의 daos_agent 는 이 날까지 2.9-gds 바이너리로 돌고 있었다(서버측 CLI 만 사용해 무해).

## 15.8 현재 환경 (다음 세션)

- **cell1/cell2: 2.8.0-rc3, provider `ofi+tcp`** — 재현되는 가장 단순한 전송이라 디버깅에
  유리해 이 상태로 남겼다. pool `gdspool`(416 GB), 컨테이너 `ci_m28`·`ci_obj`.
- rc_v 로 되돌리려면: yml provider 수정 + §15.7-3 절차(재포맷 포함).
- client-5: `/root/dfs_integrity_28`·`/root/obj_integrity_28`(2.8 링크), `-T`/`-A` 지원판.
  결과 로그: `/root/mrcab`·`/root/bufab`·`/root/procab`·`/root/bulkoff`·`/root/ofitcp`.
- 다음 작업 = **Phase 3 경계 해싱**: 서버 debug 빌드(cell1 `/root/daos-2.8` 소스, S1~S3 지점)
  + 클라 C1~C3. 그 전에 값싼 것: `ofi+tcp` 상태에서 kernel tcpdump 로 fetch bulk 페이로드를
  wire 에서 캡처해 S(송신)–C(수신) 을 코드 수정 없이 비교할 수 있다 — tcp 로 남긴 또 하나의
  이유.

---

# 16. ★★ tcpdump 페이로드 비교 — 손상은 서버가 송신 전에 만든다 (2026-09-01 심야)

§15 에서 클러스터를 `ofi+tcp` 로 남긴 이유가 이것이다: fetch 응답이 평범한 TCP 라, 계측 빌드
없이 **wire 자체를 증인으로** 세울 수 있다. 페이로드가 자기술적 8바이트 태그이므로 pcap 만으로
"누구의 어느 chunk 가 몇 바이트 지나갔는지" 셀 수 있다.

## 16.1 방법 (전부 재현 가능)

- `tests/pcap_tagscan.c`(신규): pcap 을 직접 파싱해 태그 런을 검출, (방향, tid, round, chunk)
  별 바이트 집계. 0 은 run 연장이 안 돼 자동 배제(zeros 는 스캐너에 안 보임 — 그게 판별을
  만든다). 18~20 GB pcap 을 3초에 처리(페이지 캐시).
- `tests/cap_loop.sh`(신규): 서버→클라 방향만 필터(`src host .82 or .84`)로 tcpdump 를 켠 채
  `obj_integrity` 를 반복, 깨끗한 try 의 pcap 은 즉시 삭제, 손상 try 만 보존.
- 검증된 캡처 충실도: 두 이벤트 모두 **0 packets dropped**, 비피해 (tid,round) 행이 전부
  정확히 chunk 당 4.00 MiB — 모델 오차 0.

## 16.2 zeros 변종 — 서버가 0 을 보냈다

`zeros_r25.pcap`(18 GB, 5/640 손상, 전부 라운드 25, chunk 전체가 0):

| 피해자 | 손상 chunk | wire 관측 |
|---|---|---|
| t15 | c5 | **c5 태그 0바이트**(원독+재시도 모두), 나머지 chunk 8 M |
| t4 | c4 | c4 부재, c0=4M(재시도에서 c0 도 0) |
| t2·t3·t8 | c6 | c6 부재, c1=4M(재시도에서 c1 도 0) |

앱 버퍼는 0xA5 로 오염시켜 두므로 0 이 "미기록"일 수는 없다 — **서버가 4 MiB 의 0 을 실제로
전송했다.** 재시도의 STILL WRONG 까지 wire 와 정합.

## 16.3 foreign 변종 — 서버가 남의 데이터를 보냈다

`foreign_r11.pcap`(20 GB, 66/640 손상). 라운드 11 의 결정적 쌍:

- 피해자 **t6**(c0 이 "t3 r11 off 25788416" 데이터로): 자기 c0 태그 **wire 에 0바이트**.
- 출처 **t3**(자신은 무손상·재시도 없음): c6 이 4 M 이어야 하는데 **10.81 M** —
  초과 **6.81 M = 3.41 M × 2회**, 3.41 M 은 정확히 "오프셋 25788416 → t3 객체 끝(28 M)" 크기.
  즉 t6 의 fetch 응답(원독+재시도) 두 번에 t3 의 그 구간이 통째로 실려 나갔다.
- t0(c2 피해) ↔ t13(c5 출처, 4→12 M) 쌍도 같은 산수로 맞는다.

**추가 단서**: t6 이 받은 4 M 중 3.41 M 은 t3, 잔여 0.59 M 은 또 다른 객체의 태그 —
응답이 **연속된 남의 데이터 두 도막**으로 채워졌다. 출처 오프셋(25788416, 21594112 등)은
chunk 경계도 아니다. ⇒ 서버가 **풀링된 스테이징/DMA 버퍼의 잘못된 위치에서 연속 구간을
그대로 퍼 보낸** 형상이다.

## 16.4 판정과 남은 수사 범위

> **두 변종 모두 손상은 서버 내부, 송신 이전에 발생한다. 클라이언트 수신 경로(mercury 클라,
> libfabric, 커널 TCP RX)는 무죄다.**

§15.6 의 용의자 (a)(b)(c) 중 클라이언트 측이 빠지고, 다음으로 좁혀진다:

1. **bio DMA 버퍼 오프셋/수명** — `bio_iod_prep()` 이후 채움↔`bulk_transfer_sgl()` 송신 사이.
   srv_bulk_cache bypass 는 **핸들** 캐시만 끈다: 풀링된 DMA chunk 버퍼와 오프셋 계산은
   bypass 후에도 그대로다(§15.3 과 모순 없음).
2. VOS fetch 가 biov 를 잘못 가리킴 (zeros 변종 = 미기록/미채움 구간 전송과 한 뿌리 가능).
3. mercury core 서버측 bulk 송신의 로컬 오프셋 계산.

zeros 변종의 해석도 정리된다: **hole 이 아니라 "아직 채워지지 않은(또는 0 으로 초기화된)
서버 버퍼를 송신"** — foreign 과 같은 매커니즘의 다른 단면일 개연성.

다음 단계(차기 세션): `src/bio/bio_buffer.c`(dma buffer 오프셋 회계)·`src/object/srv_obj.c`
`bulk_transfer_sgl()`·`src/vos/vos_io.c` biov 경로 코드 감사 + 서버측 S1/S2 해시 계측(이제
클라 계측 C1~C3 은 불필요). 손상 시각 대조용 원자료: client-5 `/home/cap/zeros_r25.*`,
`/home/cap/foreign_r11.*` (pcap 38 GB — 분석 후 정리 여부는 사용자 판단).

## 16.5 상류 제출용 한 줄 갱신

"클라이언트가 무엇을 하든(어느 API, 어느 캐시 설정, 어느 프로세스 구성) 무관하고, **어느
전송이든**(ucx+rc_v RDMA, ofi+tcp 소켓) 재현되며, **tcpdump 가 서버 송신 페이로드에서 이미
다른 객체의 바이트를 보여준다.** 서버 fetch 데이터패스 결함이다."

---

# 17. 서버 코드 감사 (2026-09-01) — 정상 경로는 무죄, 용의 구간은 "채움 이하"로 축소

§16 의 wire 판정("서버가 송신 전에 만든다")을 들고 서버 fetch 데이터패스를 감사했다.
대상 트리: `port/2.8-wsd`(현재 2.8 서버의 소스), `c87080a70`(지난주 2.9-gds 서버),
upstream `3604d406ef`(2.8.0-rc3)·`841487de8`(2.9.100).

## 17.1 소스 계보 발견 — 두 서버의 커스텀 패치는 서로소다

| 서버 빌드 | upstream 대비 커스텀 변경 |
|---|---|
| 2.9-gds (`c87080a70`, 지난주 전체) | **cart**(crt_bulk rkey import 배관 +165)·object(플래그/텔레메트리). **bio·vos 무변경** |
| 2.8-wsd (`port/2.8-wsd`, 오늘) | **bio**(WS-D hot staging +368)·**vos**(zfs-cap staging +3525)·vea 소폭. **cart 무변경** |

두 빌드가 같은 서명으로 손상되므로 **커스텀 패치는 어느 쪽도 원인이 될 수 없다**(교집합 없음).
⇒ 결함은 양쪽이 공유하는 것: **upstream 코어(bio/vos/vea/object), prereq(SPDK v26.01 — upstream
자체 bump, mercury 2.4.1+패치 5종), 그리고 MD-on-SSD 구성.**

주의: §12.1 의 "스톡" A/B 는 클라이언트만 스톡이었다(§12.8d 그대로). ~~서버까지 완전 upstream
인 검증은 여전히 미실시~~ → **§18 에서 실측 완료: 완전 upstream 서버에서도 동일 재현.**

## 17.2 mercury/cart bulk 코어도 배제된다 (기존 증거 재해석)

§12.5 의 엔진 로그 — **VOS aggregation 의 `csum_agg_verify()` 가 DER_CSUM 으로 실패** —
aggregation 의 내부 읽기는 **mercury/cart/bulk 를 전혀 타지 않는다**(bio 로 직접 읽음).
즉 순수 서버 내부 읽기에서 이미 깨진 데이터가 보였다. §16(전송 전 손상)과 합치면:

> **손상은 "NVMe → DMA 버퍼 채움" 구간 또는 그 이하에서 발생한다.**
> (VOS 주소해석 → VEA → bio nvme_rw → SPDK blob read → NVMe)

(단서: 그 로그는 2.9-gds 시기의 것. 2.8 에서 agg-내부-읽기 손상은 아직 재확인 안 함.)

## 17.3 정상 경로 검증 — 순서는 안전하다 (file:line)

| 검증 항목 | 결과 |
|---|---|
| fetch bulk 동기 대기 | `obj_local_rw` 는 `obj_bulk_transfer(..., p_arg=NULL)` = sync. eventual 은 부분 실패 시에도 in-flight 전부를 기다림 (`srv_obj.c` obj_bulk_comp_cb/done: 경로) |
| DMA 해제 시점 | `bio_iod_post_async()` 는 **UPDATE 전용**(`bio_buffer.c` "Async post is for UPDATE only") — fetch 는 bulk 완료 후 동기 해제 |
| NVMe 채움 대기 | upstream·2.8-wsd 모두 `dma_rw()` 꼬리에서 `if (!bd_async_post) iod_dma_wait()` — fetch 는 `bio_iod_prep()` 반환 전에 채움 완료 |
| DMA 예약 산술 | `chunk_reserve()`/`dma_map_one()` 은 yield 없이 원자적(xstream 당 협조적 스케줄링) — 이중 예약 창 없음 |
| WS-D 활성 여부 | hot_pool 미구성 → `bd_hot_ctxt == NULL` → plain 경로. `dma_rw_mixed` 미사용 |

"send-before-fill" 가설(§16 말미)은 **정상 경로에서는 기각** — 순서 보장이 코드에 있다.

## 17.4 남은 용의자 (순위·근거·판별 실험)

**S1. SPDK v26.01 blobstore read** — 상류가 최근 bump 한 새 의존성(DAOS-18943, #18172),
양 빌드 공유. cluster map 이 stale/경합이면 **미할당 cluster 읽기 = zeros, 잘못된 cluster =
foreign** — 두 변종이 한 기전으로 설명되는 유일한 후보. 내부 읽기(aggregation)도 같은 경로.
→ 판별: prereq 만 SPDK v25.x 로 내려 재빌드 A/B (엔진 재빌드 필요, cell1 에서 가능).

**S2. VEA 이중 할당/extent 겹침** — foreign 을 설명하나, at-rest 가 대체로 깨끗한 것(지속성
없음)과 부딪힘. → 판별: VEA free/alloc 에 겹침 assert 를 넣은 debug 빌드.

**S3. VOS evtree 주소의 일시적 오해석**(동시 overwrite 하) — stale-round 변종은 설명하지만
**타 객체 데이터는 구조적으로 설명 불가**(evtree 는 객체별). 하위 순위.

**S4. bio 채움 후 clobber** — 예약 산술은 결백 판정. 하위 순위.

**최우선 판별 실험(차기 세션): `bio_iod_prep()` 반환 직후 chunk 내용 해시**(서버 debug 빌드,
계획서 §5.2 의 S1 지점 하나면 충분해졌다 — §16 이 C1~C3 를, §17.2 가 S2/S3 를 제거).
- 해시가 이미 틀림 → S1/S2 (채움 이하) 확정 → SPDK 다운그레이드 A/B 로 분기
- 해시가 맞음 → S4 재부상 (채움 후 clobber)

## 17.5 감사 범위의 한계 (정직 고지)

- `bio_bulk.c` 의 bulk-group 예약 경로(`bulk_map_one`)는 정독하지 못했다 — 단 bypass arm
  (§15.3)이 그 경로 없이도 손상됐으므로 단독 원인은 아니다.
- VEA 내부(aging/reuse 창)와 SPDK blobstore 소스는 미감사 — S1/S2 판별 실험이 먼저다.
- 2.8 서버에서 agg-내부-읽기 손상 재확인(§17.2 단서) 미실시.

---

# 18. ★★★ 완전 upstream 스택에서 재현 확정 (2026-09-01 심야) — 상류 버그로 종결

사용자 지적("아까 upstream 2.8-rc3 에서 테스트 해본 거 아니었나?")이 §13 의 오기(升級)를
드러냈고, 그 구멍을 측정으로 닫았다. **이번에는 진짜 전 스택 upstream 이다.**

## 18.1 무엇이 스톡인가 (전부 검증)

| 구성요소 | 내용 | 검증 |
|---|---|---|
| 서버 코어 | `/var/daos-stockfull` = upstream `841487de8`(v2.9.100-tb 계열) | `libbio.so` 에 WS-D 문자열 **0건**(wsd RPM 은 1건) |
| 서버 prereq | mercury·UCX·SPDK·ofi 전부 `--build-deps=yes` 스톡 빌드(§12.1 의 그 빌드) | rkey 심볼 0, UCX CUDA 매크로 없음 |
| 클라이언트 | 같은 `/var/daos-stockfull` (agent·libdaos·재현기 링크) | §12.1 검증 재사용 |
| provider | `ofi+tcp` (RDMA 없음) | — |
| 컨테이너 | RP_2G4·chunk 4 MiB·rd_fac:1 (기존과 동일) | — |

## 18.2 결과 — 동일 서명, 동일 자릿수

| | 결과 |
|---|---|
| DFS 16×40 ×8 | **86/5120 = 1.68 %** (62·0·0·0·0·0·7·17 — 버스트 패턴 그대로) |
| raw obj ×3 | **59/1920** (6·7·46) |
| 서명 | 동일: chunk 하나가 **같은 라운드 다른 객체** 데이터 (`object t9's data (round 0, offset 4194264)` 등), retry STILL WRONG 다수 |

## 18.3 판정

> **ExaStor 패치는 서버·클라이언트·prereq 어디에도 원인이 없다. 이것은 순수 upstream DAOS
> (2.9.100 계열, MD-on-SSD, provider 불문)의 서버측 fetch 데이터패스 결함이다.**

§17.1 의 서로소 논증에 남아 있던 잔여 가설("서로 다른 두 커스텀 패치가 우연히 같은 서명을
만든다")까지 소멸. §17.4 의 용의자 순위(S1 SPDK v26.01 blobstore read, S2 VEA)는 그대로
유효하며, 이제 전부 **upstream 코드** 안에 있다.

상류 제출 관점에서 현재 클러스터 상태가 이상적이다: **재현기·서버·클라 전부 upstream 소스로
빌드된 상태에서 재현 중** — "귀사 코드만으로 재현된다"를 스크린샷 수준으로 보여줄 수 있다.

## 18.4 스톡 서버 배포 함정 (재현용 레시피)

1. scons 설치본에는 SPDK 스크립트가 없다 → `daos_server` auto-prepare 가
   "Could not find the SPDK setup.sh script" 로 실패. **빌드 트리에서
   `external/release/spdk/{scripts,include/spdk}` 를 `<prefix>/share/daos/spdk/` 로 복사**
   (setup.sh 는 `../include/spdk/pci_ids.h` 를 요구한다).
2. `daos_server_helper` 는 **root:daos_server + setuid(4750)** 필요(RPM 과 동일하게).
3. client-5 는 SELinux Enforcing — `/var` 아래 바이너리(var_t)를 systemd 가 실행 거부(203/EXEC).
   `chcon -R -t bin_t <prefix>/bin` + lib 는 lib_t.
4. helper 버전 불일치 주의: 셸 PATH 에 /usr/bin 이 앞서면 2.8 helper 를 집는다("version
   mismatch server 2.9.100 / helper 2.8.0"). systemd drop-in 의 PATH 를 prefix 우선으로.
5. cell1 wedge 레시피(§15.7)는 스톡 서버에서도 동일하게 유효했다.

## 18.5 현재 환경

- cell1/cell2: **완전 스톡 2.9.100 서버**(`/var/daos-stockfull`), provider `ofi+tcp`,
  pool `gdspool`(SCM 8G+NVMe 200G/rank), 컨테이너 `ci_m28`·`ci_obj`. 양 rank Joined.
- client-5: 스톡 agent(systemd, SELinux 라벨 수정됨), `/root/dfs_integrity_stock`·
  `/root/obj_integrity_stock`(둘 다 stockfull 링크). 결과: `/root/stocksrv/`.
- ExaStor 빌드로 복귀: drop-in 을 `/usr/bin/daos_server`(2.8-wsd RPM) 또는
  `/opt/daos-gds/bin`(2.9-gds) 로 + 재포맷.

---

# 19. ★★★ 서버 계측 확정 — 손상은 NVMe→DMA 채움에서 발생 (2026-09-01 심야)

계획서 §5.2 의 서버측 해시 지점을 **하나로 압축**해 계측했다. 완전 upstream 스택(§18)에
디버그 패치를 얹어 fetch 버퍼를 **bulk 전송 직전**에 감사했다.

## 19.1 계측 (upstream `841487de8` + 디버그, dev 박스 소스 반영)

- `src/object/srv_obj.c`: `obj_local_rw()` 의 `bio_iod_prep()` 성공 직후, fetch 이면
  `fillhash_check_sgl()` 로 각 iod 의 bio SGL 을 감사. env `FILLHASH_DEBUG=1` gate.
- **핵심 설계 교정**: 처음엔 bio 계층(`bio_buffer.c`)에서 버퍼 자기일관성만 봤는데,
  **chunk 전체가 통째로 다른 객체로 치환되면 자기일관(전 워드 동일 tid·단조 offset)이라
  검출 못 함**. → OID 를 아는 object 계층으로 옮기고, obj_integrity 가 객체를
  `oid.lo = 0xC0FFEE00 + tid` 로 만드는 것(set_oid 는 lo 보존)을 이용해 **기대 tid 대조**로
  전환. bio 계측은 원복.
- 빌드: `/var/daos-stockfull` 증분(`scons --build-deps=no`), `libobj.so` 재링크·양 cell 배포.

## 19.2 결과 — 채움 직후 버퍼가 이미 틀렸다

client 65/640 손상과 **같은 시각**, 양 rank 엔진 로그:

```
srv_obj.c:307 fillhash_check_sgl() FILLHASH <oid> iod0 iov0 exp_tid=7 len=4194304:
    foreign=524288 zero=0 offbad=0, first bad at 0 val t14 r0 off 4194304
    (buffer wrong BEFORE bulk send)
```

- **cell1 107건 + cell2 147건**, 전부 `foreign=524288` = **4 MiB 워드 전량**이 남의 데이터.
  즉 fetch 한 chunk 버퍼가 **통째로 다른 객체의 chunk 로 채워졌다**(exp t7 → t14 의 off
  4194304 데이터, 객체도 offset 도 다름).
- `zero=0` — 이 라운드엔 zeros 변종 없음(foreign 변종만).
- 위치: `bio_iod_prep()` 반환 직후 = **NVMe→DMA 채움 완료 시점**. mercury·cart·bulk·RDMA·
  클라이언트 수신 경로는 **아직 실행되지도 않았다.**

## 19.3 판정 — 분기 트리 종료

> **손상은 서버의 fetch 채움 경로에서 발생한다: VOS extent 주소해석 → VEA → SPDK blobstore
> read → NVMe. bulk/전송/클라이언트는 전부 무죄(코드가 아직 안 돎).**

§17.4 의 남은 용의자 S1(SPDK v26.01 blobstore read)·S2(VEA 이중할당)만 남고, "채움 후
clobber"(S4)는 계측으로 제거. §19.2 의 "chunk 전체 = 다른 객체" 형상은 S1(잘못된 cluster
매핑)·S2(extent 겹침) 둘 다와 부합. 다음 판별:
- **SPDK v26.01 → v25.x 다운그레이드 A/B**(prereq 만 교체 재빌드): clean 이면 S1 확정.
- VEA alloc/free 겹침 assert 디버그 빌드: 걸리면 S2 확정.

## 19.4 NVMe 물리 교차 점유는 아님 (사용자 질의 확인)

`dmg storage query list-devices`: **8 NVMe ↔ target 0–7 이 1:1**, 공유 없음. 따라서 "다른
객체 데이터"는 **두 SSD 가 서로 새는 것이 아니라 한 target 내부**(같은 SSD 의 blob 공간에
여러 객체 chunk 공존)에서 cluster 오매핑/extent 겹침으로 발생. → S1/S2 와 일치. 소스라우팅은
양 cell 에 적용 유지 확인(정책라우팅 100/101, arp_ignore/announce, rp_filter=2) — fetch 채움
결함과는 무관(서버 내부라 네트워크 이전 문제).

## 19.5 환경/자산

- 디버그 서버 실행중: `/var/daos-stockfull`(upstream + fillhash 패치), `FILLHASH_DEBUG=1`
  양 cell yml. provider ofi+tcp, pool gdspool, 컨테이너 ci_m28·ci_obj.
- 디버그 패치 소스: dev 박스 `~/src/Flexa/daos` (branch `port/2.8-wsd` 워킹트리 —
  srv_obj.c 에 fillhash_check_sgl/enabled, **커밋 안 함**. cell1 `/var/daosbuild/daos-stock`
  에 동일 패치 적용본). 원복하려면 `/tmp/srv_obj.c.orig` 복원 후 재빌드.
- 결과 로그: client-5 `/root/fh2/`, 서버 `/var/log/daos/daos_engine.0.log`.

---

# 20. ★★★ 손상이 안 나는 대조 클러스터 — 차이는 bio 백엔드 class (2026-09-01)

사용자 제공: **192.168.34.30/31/32(ExaCI4)에서는 재현 안 됨.** 접속 `root/gluesys!!`(직결,
dev 박스에서). 34.30=client(`ExaCI4-3J`), 34.31=server(`FlexA_3433_1-A`), access_points=[34.31].

## 20.1 두 환경 비교

| 축 | 우리(손상) | 34.x(정상) |
|---|---|---|
| DAOS | source `841487de8`(+wsd/gds) 및 stockfull | RPM `2.9.100-4.exastor.402.g64a818563` |
| **bio class** | **`nvme` (SPDK userspace, vfio-pci)** | **`kdev` (커널 블록 디바이스)** |
| vfio 바인딩 | 8개 | **0개** |
| 데이터 디바이스 | 실 PASCARI NVMe ×8 | QEMU 가상 NVMe(1b36) + **zvol** `/dev/zvol/daoshdd/daosdata` |
| bdev_roles | (ram=meta) + (nvme=data) 분리 | `[wal,meta,data]` 단일 디바이스 |
| targets/engine | **8** | **1** |
| provider | ucx+rc_v → ofi+tcp | ofi+verbs;ofi_rxm |
| **SPDK** | **v26.01** | **v26.01 (daos-spdk-26.01-2)** ← 동일 |
| 하드웨어 | 베어메탈 | VM |

## 20.2 해석 — SPDK 버전이 아니라 SPDK userspace NVMe 경로

**양쪽 SPDK 26.01 동일** → §17.4/§19 의 "SPDK v26.01 회귀" 프레임은 **버전 문제가 아니다**로
정제. 남는 최유력 단일 차이는 **bio class: `nvme`(SPDK vfio userspace blobstore-over-raw-NVMe)
vs `kdev`(커널 블록)**. 우리 §19 결론(손상 = NVMe→DMA 채움, 즉 blobstore read)과 정확히
정합: **kdev 는 SPDK userspace NVMe 읽기 경로를 통째로 우회**하므로 손상이 안 나는 것과 부합.

단 34.x 에는 교란요인이 많다(targets 1 vs 8, VM 가상디스크, provider verbs, RPM 빌드 상이).
1-target VM 이 애초에 우리의 동시성/멀티타깃 부하를 못 만든다는 가능성도 배제 못 함. 그래서
34.x 는 "kdev 가 원인"의 증명이 아니라 **강한 정황 + 단일변수 실험 설계의 근거**다.

⚠️ 관측 시점: 34.31 daos_server 는 **현재 inactive**(구동 안 함). 사용자의 "문제 없음"은
과거 구동 시 관측으로 이해. config 비교는 유효.

## 20.3 결정적 단일변수 실험 (다음 세션)

우리 클러스터에서 **다른 건 모두 고정하고 bio class 만 `nvme`→`kdev` 로**:
1. `dmg storage` 정지 → vfio 에서 NVMe 언바인드 → 커널 nvme 드라이버로 복귀
   (`/dev/disk/by-id/nvme-...`).
2. server.yml storage tier 를 `class: kdev` + `bdev_list:[by-id 경로들]` 로. SCM=ram 유지.
3. 재포맷 → 같은 obj_integrity/FILLHASH 배터리.
- **kdev 에서 clean → SPDK userspace NVMe 읽기 경로(S1) 확정.** upstream 제출의 핵심 재현
  경계가 된다("class:nvme 에서만, class:kdev 에선 안 남").
- kdev 에서도 손상 → SPDK 아래(VEA/VOS 주소해석, S2) 또는 targets≥2 조건으로 범위 이동.

보조 실험(교란 분리): 우리 클러스터를 **targets:1** 로 줄여도 나는지(멀티타깃이 조건인지),
그리고 34.x 서버를 다시 띄워 **targets 를 8 로 올리고 class 를 nvme(가능하면)로** 바꿔 재현
시도. 다만 34.x 는 가상디스크라 SPDK nvme class 부적합할 수 있음.

## 20.4 부수 확정
- **SPDK 버전은 범인이 아니다**(양쪽 26.01). §19.3 의 "SPDK v25 다운그레이드 A/B"는 우선순위
  강등 — 대신 **class kdev A/B** 가 1순위.
- 34.x 는 이전 세션이 "대조군으로 쓰지 말라"던 CI(35.x)와 같은 계열(VM·verbs·targets1·zvol)
  이나 IP·빌드가 다름. verbs 라서가 아니라 **kdev·1-target 이라 안 나는 것**으로 재해석.

---

# 21. class:kdev A/B — arm A 확정, arm B 는 구성 장벽으로 미완 (2026-09-01)

§20 의 1순위 실험(`class: nvme` → `kdev` 단일변수)을 시도했다. **결론: arm B 를 우리 하드웨어에
세울 수 없었다.** 원인 격리는 진전 없음, 대신 계측의 적용 범위와 kdev 구성 요건을 배웠다.

## 21.1 arm A (class:nvme) 기준선 — 같은 세션 대조로 확보

| 워크로드 | 클라이언트 손상 | 서버 FILLHASH(실손상) |
|---|---|---|
| obj_integrity ×8 (16×40) | **67/5120** | **171건** (cell1 108 + cell2 63) |
| dfs_integrity ×6 (16×40) | 148/3840 | **0건** |

**중요 — FILLHASH 는 obj_integrity 에만 유효하다.** 계측이 `oid.lo = 0xC0FFEE00+tid` 센티넬로
기대 tid 를 얻으므로, DFS 객체(센티넬 없음)에서는 조용히 건너뛴다. DFS 손상 148건에 FILLHASH
0건인 것은 "DFS 는 채움이 정상"이 아니라 **검사 미적용**이다. §19 의 결론은 obj 경로 실측이라
그대로 유효하되, DFS 경로의 채움 단계 검증은 별도 계측이 필요하다(오해 방지).

**오탐 1종 확인**: `foreign=0 zero=1 offbad=0` 은 t0 객체 offset 0 의 첫 워드가
`tag_of(0,0,0)==0` 이라 0 과 구별되지 않는 것 — 진짜 손상 아님. 집계에서 제외해야 한다.

## 21.2 arm B (class:kdev) — 세우지 못함

시도한 것과 각 단계의 벽:

1. vfio → 커널 드라이버 복귀(setup.sh reset), by-id 경로 8개 확인 → OK.
2. yml `class: nvme` → `class: kdev` + `bdev_list:[by-id 8개]` (targets 8 유지) → 기동 시
   `bio_xstream.c:584 subsys_init_cb() subsystem init failed: -22`(EINVAL) →
   `failed to init bdevs: DER_INVAL`.
3. 로그의 `bdev_name2roles() bdev name:AIO_cell1_N_1_0, bdev role:0` 을 근거로 34.x 처럼
   `bdev_roles: [wal, meta, data]` 추가 → `SCM format required` 로 진행(MD-on-SSD 로 인식),
   `control_metadata: path:` 도 추가 → 포맷은 8 디바이스 성공.
4. 그러나 엔진은 여전히 `failed to init spdk context ... DER_INVAL(-1003)`.

미해결 가설(다음 세션):
- **4 KiB 논리 섹터**: 우리 NVMe 는 `logical_block_size=4096`, 34.x 는 512 B QEMU 디스크.
  SPDK AIO 자체 검사(512 이상·2^n)는 통과하므로 상위(bio blob/cluster 정렬, WAL 요건)에서
  거부되는 것으로 의심. `block_size` 명시 주입 경로가 DAOS yml 에 없음.
- **8 디바이스에 wal+meta+data 동시 롤**: 34.x 는 디바이스 1~2개. 롤 분리(예: 1개 wal/meta,
  나머지 data)로 재시도할 가치 있음.
- `class: file`(sparse file bdev)로 대체하면 SPDK userspace NVMe 경로를 우회하면서 4K 문제를
  피할 수 있어, **kdev 대신 file 로 같은 판별을 얻는 우회로**가 유력하다.

## 21.3 판정과 다음 순서

- §20 의 "kdev 가 clean 의 원인" 가설은 **여전히 미검증**. 34.x 는 교란요인(targets 1, VM,
  512 B, 단일 디바이스, verbs, 다른 RPM)이 많아 정황 이상으로 못 쓴다.
- 우선순위 재조정:
  1. **`class: file` A/B** — SPDK userspace NVMe 우회를 4K 섹터 문제 없이 달성(파일 bdev).
     clean 이면 §19 의 "채움 = SPDK NVMe read" 를 강하게 지지.
  2. **targets 8 → 1** 단일변수(멀티타깃이 조건인지) — 구성 변경이 가벼움.
  3. kdev 재도전: 롤 분리 + 디바이스 수 축소.
- 환경은 **arm A(class:nvme)로 원복 완료**: 양 rank Joined, pool gdspool, ci_m28·ci_obj 재생성,
  fillhash 계측 서버 유지. 백업 `/root/daos_server.yml.nvme-arm`(양 cell).

---

# 22. 정정: 34.x 는 nvme blob 으로도 통과 — kdev 가설 폐기, 동시성 축도 배제 (2026-09-02)

사용자 보고: **34.x 에서 `class: nvme`(SPDK blob) 구성으로도 정상 통과.** §20/§21 의
"kdev 가 차이" 가설은 **폐기**한다.

## 22.1 34.x 가 통과시킨 실제 구성 (`/etc/daos/daos_server_nvme_blob.yml`, 9/1 23:45)

```yaml
disable_vfio: true        # ← UIO, not VFIO
disable_hotplug: true
nr_hugepages: 2048
control_metadata: {path: /var/daos/control_meta_nvme_blob_20260901}
engines:
- targets: 1              # ← 우리 8
  nr_xs_helpers: 0        # ← 우리 2
  storage:
  - {class: ram, scm_size: 4}
  - {class: nvme, bdev_list: ['0000:00:03.0','0000:00:04.0'],
     bdev_roles: [wal, meta, data]}   # ← MD-on-SSD 롤; 우리는 롤 없는 ram+nvme 분리
```
디바이스는 **QEMU 가상 NVMe 2개, 논리섹터 512 B**(lspci 실 NVMe 0개). provider ofi+verbs;ofi_rxm.

## 22.2 동시성 축(targets/helpers) 실측 — 조건 아님, 오히려 악화

우리 하드웨어에서 그들의 동시성 설정만 맞췄다(`targets: 1`, `nr_xs_helpers: 0`, 나머지 고정).
2-target 풀이 되므로 컨테이너·객체 oclass 를 **RP_2G1** 로(oclass 는 §12.4 에서 비-gate 확정).

| arm | 클라이언트 | 서버 FILLHASH |
|---|---|---|
| targets 8 / helpers 2 (§21.1) | 67/5120 = **1.3 %** | 171 |
| **targets 1 / helpers 0** | **271/2560 = 10.6 %** | **562** (cell1 134 + cell2 428) |

⇒ 서버 동시성(멀티타깃·helper offload)은 **원인도 조건도 아니다.** 단일 target·offload 없음에서
오히려 5배 심해졌다(부하가 한 xstream 에 집중되어 노출이 커진 것으로 해석). 서명 동일
(`t4` 의 chunk0 이 `t2 r1 off 4194304` 데이터로, foreign=524288).

## 22.3 남은 차이 축 (우선순위 재정렬)

| 축 | 우리(손상) | 34.x(정상) | 평가 |
|---|---|---|---|
| **논리 섹터** | **4096 B** (실 PASCARI) | **512 B** (QEMU) | ★ 최우선. blob cluster/정렬 산술이 4K 에서만 깨질 수 있음. §21.2 의 kdev EINVAL 도 4K 정황 |
| **디바이스 수** | 8 | 2 | ★ blob 이 여러 디바이스에 걸칠 때만? |
| **bdev_roles** | 없음(ram SCM + nvme data 분리) | `[wal,meta,data]` MD-on-SSD | ★ 메타데이터 위치가 다름 = VOS/blob 레이아웃 상이 |
| disable_vfio | false(VFIO) | **true(UIO)** | 중. SPDK DMA 매핑 경로 상이 |
| 하드웨어 | 베어메탈 실 NVMe | VM 가상 NVMe | 중(가상 디스크가 결함을 감출 수 있음) |
| DAOS 빌드 | source `841487de8` | RPM `exastor.402.g64a818563` | 중. 커밋 미확인(로컬 트리에 없음 — fetch 필요) |
| targets/helpers | 8/2 | 1/0 | **배제(§22.2)** |
| provider | ofi+tcp | ofi+verbs;ofi_rxm | **배제(§15.5)** |
| oclass | RP_2G4/RP_2G1 | RP_2G1 | **배제(§12.4)** |
| SPDK 버전 | v26.01 | v26.01 | **동일** |

## 22.4 다음 실험 순서

1. **`bdev_roles: [wal,meta,data]` + 디바이스 2개**로 34.x 레이아웃 모방(우리 하드웨어).
   clean 이면 "메타데이터 위치/디바이스 수" 축, 계속 손상이면 하드웨어(4K/실NVMe)로 좁혀진다.
2. **`disable_vfio: true`(UIO)** 단일변수.
3. **4K vs 512B**: 우리 SSD 를 512 B 포맷으로 재구성(`nvme format --lbaf`)하거나, 34.x 에
   4K 가상 디스크를 붙여 재현 시도 — 이 축이 남으면 사실상 결정적.
4. exastor RPM 커밋 `64a818563` fetch 후 `841487de8` 와 bio/vos/vea diff.

환경 현재: **targets 1 / helpers 0, RP_2G1 컨테이너**로 두었다(손상률 10.6 % 로 재현이 빨라
후속 A/B 에 유리). 8/2 복귀는 `/root/daos_server.yml.nvme-arm` 백업 참조.

## 22.5 34.x 저장 레이아웃 모방 실측 — 이 축도 배제 (오히려 24 %)

§22.4-1 실행: 우리 하드웨어에 34.x 의 레이아웃을 맞췄다 — **디바이스 2개**(0000:02·03:00.0),
**`bdev_roles: [wal, meta, data]`**(MD-on-SSD), `control_metadata`, **scm_size 4**,
targets 1 / helpers 0 유지.

| arm | 클라이언트 손상 | 서버 FILLHASH |
|---|---|---|
| targets 8/helpers 2, 8dev, 롤 없음(ram SCM+nvme data) | 67/5120 = 1.3 % | 171 |
| targets 1/helpers 0, 8dev, 롤 없음 | 271/2560 = 10.6 % | 562 |
| **targets 1/helpers 0, 2dev, MD-on-SSD 롤** | **618/2560 = 24.1 %** | **1680** (644+1036) |

서명 동일(`t12` chunk5 ← `t10 r0 off 25165824`, foreign=524288, retry STILL WRONG).

⇒ **저장 레이아웃(디바이스 수·메타데이터 위치/롤)도 원인이 아니다.** 34.x 구성을 하나씩 맞출
때마다 손상률이 **1.3 % → 10.6 % → 24.1 %** 로 올라갔다 — 즉 34.x 가 통과하는 이유는
**소프트웨어 구성이 아니다.** (구성을 좁힐수록 노출이 커지는 방향이므로, 34.x 의 통과는 구성이
아닌 다른 요인 덕이다.)

### 소프트웨어 구성 축 소진 — 남은 것은 하드웨어/빌드

| 축 | 상태 |
|---|---|
| targets/helpers, 디바이스 수, bdev_roles/메타 위치, oclass, provider, SPDK 버전, bio class(kdev 시도) | **전부 배제 또는 무관** |
| **논리 섹터 4096 B vs 512 B** | ★ 미검증 — 최우선 |
| **실 NVMe vs QEMU 가상 NVMe** | ★ 미검증 (가상 디스크가 결함을 감출 가능성) |
| disable_vfio(UIO) | 미검증 (SPDK DMA 매핑 경로) |
| DAOS 빌드 `exastor.402.g64a818563` vs `841487de8` | 미검증 (커밋 로컬에 없음) |

### 다음 실험 (개정)
1. **`disable_vfio: true`(UIO)** — 가장 값싼 남은 단일변수(yml 한 줄, 재포맷 불필요할 수도).
2. **512 B 재포맷**: `nvme format -l <lbaf_512>` 로 데이터 SSD 1~2개를 512 B 로 바꿔 A/B.
   PASCARI 가 512 B lbaf 를 지원하는지 `nvme id-ns` 로 먼저 확인. **디스크 내용 파기됨**.
3. exastor RPM 커밋 fetch 후 bio/vos/vea diff (그 빌드에 수정이 들어있을 가능성).
4. 34.x 에 **4 KiB 가상 디스크**를 추가해 거기서 재현되는지 — 역방향 검증으로 가장 결정적.

환경: **targets 1/helpers 0, 2dev, MD-on-SSD, RP_2G1, pool 206 GB** 유지(손상률 24 % 로 A/B 가
가장 빠름). 이전 arm 백업 `/root/daos_server.yml.nvme-arm`(8dev/8targets), `/root/daos_server.yml.t1arm`.

---

# 23. ★★ 역방향 검증: 34.x 를 4 KiB 섹터로 바꿔도 통과 — 섹터 크기 배제 (2026-09-02)

§22.5 의 §22.4-4("34.x 에 4 KiB 디스크를 붙여 재현 시도")를 실행했다. **우리 장비를 파괴적으로
재포맷할 필요가 없었다**: 34.31 의 QEMU NVMe 가 `LBA Format 4 = 4096 B` 를 지원해 그 자리에서
바꿀 수 있었다.

## 23.1 절차 (재현용)

```bash
# 34.31 (root/gluesys!!, dev 박스 직결). 두 데이터 디바이스 = 0000:00:03.0/04.0 = nvme0/1
nvme id-ns /dev/nvme0n1 -H | grep "LBA Format"   # lbaf 4 = 4096B 지원 확인
nvme format /dev/nvme{0,1}n1 --lbaf=4 --force    # 512B -> 4096B (내용 파기)
cat /sys/block/nvme0n1/queue/logical_block_size   # 4096 확인
```
기동 함정 4건: ① `ib0` 에 IPv4 없음 → `fabric_iface: ens19`(10.10.34.31), provider 는
§15.5 로 비-gate 이므로 `ofi+tcp` 로 대체 ② `/var/run/daos_server` 디렉터리 필요
③ nohup/setsid 로는 ssh 종료 시 죽음 → `systemd-run --unit=... --collect`
④ 34.31 은 daos-devel 없음 → cell1 의 `/var/daos-stockfull/include` 를 복사하고
`libuuid-devel` 설치, `.so` 심볼릭 없어 `libdaos.so.2`·`libgurt.so.4`·`libdaos_common.so`
직접 링크.

구성: `class: nvme`(SPDK), 디바이스 2개 **4096 B**, `bdev_roles:[wal,meta,data]`,
targets 1 / helpers 0, `disable_vfio: true`, pool `p4k` 63 GB, 컨테이너 SX(단일 rank).

## 23.2 결과 — 4 KiB 에서도 통과

| arm | 결과 |
|---|---|
| 34.31, 4 KiB 섹터, 16×20 ×3 | **0/960** |
| 34.31, 4 KiB 섹터, 16×40 ×4 | **0/2560** |

⇒ **논리 섹터 크기는 원인이 아니다.** §22.5 의 최우선 가설 기각. (§21.2 의 kdev EINVAL 은
별개의 구성 문제였을 뿐 손상과 무관.)

## 23.3 남은 차이 축 — 두 개로 좁혀졌다

소프트웨어 구성(§22.2·§22.5)과 섹터 크기(§23.2)가 모두 배제된 뒤 남은 것:

| 축 | 우리(손상 1.3~24 %) | 34.x(0/3520) | 비고 |
|---|---|---|---|
| **DAOS 빌드** | source `841487de8`(upstream) | RPM **`exastor.402.g64a818563`** | 커밋이 우리 트리에 없음 → **그 빌드에 수정이 들어있을 가능성** |
| **하드웨어/플랫폼** | 베어메탈, 실 PASCARI NVMe ×2~8, EPYC 다중 NUMA | **KVM VM**, QEMU 가상 NVMe, 단일 NUMA | 가상 디스크가 결함을 감출 수 있음(타이밍·큐 깊이·DMA 경로) |
| disable_vfio | false(VFIO) | **true(UIO)** | 아직 미검증 — 값싼 단일변수 |

## 23.4 다음 실험 (개정, 값싼 순)

1. **우리 클러스터에 `disable_vfio: true`(UIO)** — yml 한 줄. clean 이면 VFIO/IOMMU DMA 경로가
   조건(플랫폼 축과 연결).
2. **exastor 커밋 `64a818563` 확보** — `git fetch gitlab`(exastor/daos) 후
   `841487de8` 와 `src/{bio,vos,vea,object}` diff. 수정이 있으면 그것을 우리 소스빌드에
   cherry-pick 해 A/B → 확정되면 상류 제출은 "이미 고쳐진 버그" 로 프레임이 바뀐다.
3. **34.x 에 우리 빌드 투입**(반대 방향): 34.31 에 `/var/daos-stockfull` 서버를 올려
   같은 VM 에서 재현되는지. 재현되면 **빌드 차이가 원인**으로 확정, 안 되면 플랫폼 축.

3번이 가장 결정적이다 — 같은 VM·같은 디스크에서 빌드만 바꾸는 단일변수다.

## 23.5 34.x 환경 상태 (원복 필요 항목)
- **두 QEMU NVMe 를 4096 B 로 재포맷했다**(원래 512 B). 원복: `nvme format --lbaf=0`.
- 추가한 것: `/etc/daos/daos_server_4k.yml`, `/etc/daos/daos_agent_4k.yml`,
  transient 유닛 `daos-srv4k`·`daos-agent4k`(둘 다 실행중), pool `p4k`, 컨테이너 `ci_obj`,
  `/root/{obj_integrity,obj_integrity.c,dh/}`, `libuuid-devel` 설치.
- 사용자의 원본 `daos_server_nvme_blob.yml`·`daos_control_nvme_blob.yml` 은 **그대로 보존**.

---

# 24. 빌드 축 배제 — 34.x 의 커밋에 수정은 없다 (2026-09-02)

§23.4-2 실행. `git fetch gitlab` 로 34.x RPM 의 커밋 **`64a818563b`**("ci(flexa): RPM 버전
가드가 set -e 로 죽던 회귀 수정 (#400)")을 확보했다. 우리 빌드(`841487de8`) 대비
**81 커밋 앞**(우리가 16 앞 — 분기 관계).

## 24.1 데이터패스 diff

| 경로 | 우리 → 34.x |
|---|---|
| **`src/bio`** | **완전 동일** (0 변경) |
| **`src/vea`** | **완전 동일** (0 변경) |
| `src/vos` | 15 files, 793+/56− |
| `src/object` | 11 files, 398+/22− |
| `src/common` | 5 files, 152+/1− |

**§19 가 손상을 확정한 채움 경로(`bio_buffer.c`·`bio_bulk.c`·`vea_alloc.c`)는 두 빌드가
바이트 동일하다.**

vos/object 변경의 실체(커밋 로그 + diff 육안 확인):
- **프로젝트 쿼터(Lustre projid)** 기능 일습 — `ic_projid`/`ic_proj_held` 추가,
  `vos_update_begin()` 시그니처에 projid 추가, `ds_obj_reproject_handler()` 신규,
  `DAOS_PROP_CO_SPACE_LIMIT/SPACE_AMP` 컨테이너 속성.
- **flat-dkey 존재확인 fetch 크래시 가드**(`iod_nr == 0` 일 때 `ic_iods[0]` 역참조 방지) —
  우리 워크로드는 항상 `iod_nr == 1` 이므로 무관.
- DTX 커밋 블롭 ENOSPC 폴백 수정.

데이터 이동 키워드(`biov|bio_iod|dma|bulk|blob|cluster`) 검색 결과 4건은 전부 주석 또는
DTX 커밋 블롭(쿼터/ENOSPC) 문맥 — **fetch 데이터 흐름 변경 0건.**

## 24.2 판정

> **34.x 가 통과하는 이유는 빌드가 아니다.** 그 빌드는 손상 경로를 우리와 동일한 코드로
> 갖고 있고, 추가된 것은 쿼터·크래시가드·DTX ENOSPC 뿐이다. "이미 고쳐진 버그" 프레임은
> 성립하지 않으며, 상류 제출은 그대로 유효하다.

## 24.3 남은 축은 하나 — 플랫폼

§22(구성)·§23(섹터)·§24(빌드)가 모두 배제됐다. 남은 차이:

| | 우리(손상 1.3~24 %) | 34.x(0/3520) |
|---|---|---|
| 플랫폼 | 베어메탈 EPYC, 다중 NUMA | **KVM VM, 단일 NUMA** |
| 디스크 | 실 PASCARI NVMe(4 K/512 무관 — §23) | **QEMU 가상 NVMe** |
| DMA 바인딩 | VFIO | **UIO (`disable_vfio: true`)** |

**다음 실험 순서(개정)**
1. **`disable_vfio: true`(UIO)** 를 우리 클러스터에 — 남은 축 중 유일하게 값싼 단일변수.
   clean 이면 **VFIO/IOMMU DMA 매핑 경로**가 조건 → 상류 이슈의 재현 조건이 크게 좁혀진다.
2. **34.31 에 우리 stockfull 서버 투입**(§23.4-3) — 이제 빌드가 배제됐으니 이 실험의 의미는
   "같은 VM 에서 우리 빌드도 clean 인가" 확인(플랫폼 축 확정용 음성 대조).
3. 실 NVMe vs 가상: 우리 클러스터에 `class: file`(sparse file) 로 가상 디스크 흉내 → clean 이면
   실 NVMe 하드웨어/드라이버 상호작용으로 좁혀진다.

---

# 25. UIO arm 시도 — 우리 하드웨어에서 구조적으로 불가 (2026-09-02)

§24.3-1 실행: `disable_vfio: true`(UIO) 단일변수.

## 25.1 진행과 벽

1. yml 에 `disable_vfio: true` 추가 → 기동 실패:
   `code = 614 "disable_vfio: true in config while running as non-root user with NVMe devices"`.
   ⇒ **UIO 는 daos_server 를 root 로 돌려야 한다**(34.x 는 수동 root 실행이었다).
2. systemd drop-in 에 `User=root/Group=root` 추가 → **UIO 바인딩 성공**
   (`uio_pci_generic` 2개, vfio 0개).
3. 그러나 포맷 시 `EAL: Bus (pci) probe failed` →
   `NVMe SSDs [0000:02:00.0 0000:03:00.0] not found`.

## 25.2 원인 — MSI-X

```
lspci -vv 0000:02:00.0 → Capabilities: MSI-X: Count=257
modinfo uio_pci_generic → "Generic UIO driver for PCI 2.3 devices"
```
`uio_pci_generic` 은 **PCI 2.3 legacy INTx 전용**이라 MSI-X 257 벡터를 쓰는 PASCARI NVMe 를
DPDK 가 probe 하지 못한다. DPDK 로 MSI-X 장치를 UIO 로 쓰려면 `igb_uio`(out-of-tree)가
필요한데 이 커널엔 없다. 34.x 의 QEMU 가상 NVMe 는 MSI-X 요구가 가벼워 통과했던 것.

⇒ **UIO 축은 우리 하드웨어에서 시험 불가**(실 NVMe + in-tree 커널 조합의 구조적 제약).
"VFIO 가 조건인가"는 여전히 미검증이며, **34.x 의 UIO 통과는 하드웨어(가상 NVMe) 덕이라
UIO 자체의 공로로 볼 근거도 없다.**

## 25.3 환경 원복
`/root/daos_server.yml.vfio-arm` 복원, drop-in 의 root 오버라이드 제거, VFIO 재바인딩,
재포맷 → 양 rank Joined(2 디바이스, MD-on-SSD, targets 1/helpers 0 = 24 % arm 유지).

## 25.4 다음 실험 (남은 것)

1. **34.31 에 우리 stockfull 서버 투입** — 같은 VM·같은 가상 디스크에서 **빌드만 우리 것**으로.
   §24 로 빌드가 배제됐으니 예상은 clean 이고, 그러면 **플랫폼(가상 NVMe/VM)이 조건**으로 확정.
   34.31 에 이미 헤더·재현기·systemd 유닛 절차가 준비돼 있어 값이 싸다.
2. **우리 클러스터에 `class: file`** (sparse file bdev) — 실 NVMe 를 파일로 대체해 SPDK
   userspace 경로는 유지하되 하드웨어를 제거. clean 이면 **실 NVMe 드라이버/디바이스 상호작용**
   으로 좁혀지고, 손상되면 **SPDK blobstore 로직 자체**로 좁혀진다. §21.2 의 kdev 와 달리
   file bdev 는 4 K 섹터 제약이 없어 성립할 가능성이 높다.
3. 실 NVMe 축이 남으면: 다른 모델/다른 서버의 실 NVMe 에서 재현 시도(하드웨어 일반성 확인).

2번이 가장 정보량이 크다 — "SPDK 로직 vs 실 하드웨어"를 우리 장비 안에서 가른다.

---

# 26. ★★★ 빌드 스왑 결정 실험 — 우리 빌드도 34.x VM 에서 깨끗: 플랫폼이 조건 (2026-09-02)

§25.4-1 실행. **같은 VM·같은 가상 디스크·같은 구성에서 서버 빌드만 우리 것으로** 바꾸는
단일변수 실험. §24 에서 빌드가 배제됐으므로 이건 플랫폼 축을 확정하는 음성 대조다.

## 26.1 절차

1. cell1 `/var/daos-stockfull`(519 MB, tar 177 MB) → dev 박스 경유 → 34.31 `/var` 에 전개.
   **FILLHASH 계측 포함 확인**(`libobj.so` 에 문자열 2건), `daos_server_helper` setuid 복원.
2. 그들 유닛(`daos-srv4k`) 정지 → `/etc/daos/daos_server_ourbuild.yml`(그들 4K 구성 복사 +
   경로만 분리 + `FILLHASH_DEBUG=1`) 로 우리 서버를 `systemd-run --unit=daos-ours` 로 기동.
3. 함정 2건: ① 이전 arm 들의 tmpfs 3개(`/mnt/daos{0,1,_nvme_blob0}`)가 RAM 을 잡아
   `MemAvailable 3.3 GiB < 3.6 GiB` 로 포맷 거부 → umount + drop_caches 로 9 GiB 확보.
   ② `scm_size: 2` 는 최소값 미달(code 730) → 4 유지.
4. 포맷 → rank 0 Joined, pool `pours` 63 GB, 컨테이너 `ci_obj`(SX), 재현기를 우리 lib 로 재링크.

## 26.2 결과

| 서버 빌드 | 플랫폼 | 결과 |
|---|---|---|
| 34.x exastor RPM `64a818563` | 34.31 VM, 가상 NVMe | 0/3520 (§23) |
| **우리 stockfull `841487de8` + fillhash** | **34.31 VM, 같은 가상 NVMe** | **0/960 → 0/3520, FILLHASH 0건** |
| 우리 stockfull `841487de8` + fillhash | **cell1/cell2 베어메탈, 실 NVMe** | **1.3 % → 24 %**, FILLHASH 171~1680건 |

⇒ **빌드는 완전히 무죄다.** 같은 소스·같은 바이너리가 VM 에서는 3520 읽기 무손상, 베어메탈
에서는 최대 24 % 손상. **차이는 플랫폼(가상 NVMe/VM) 뿐이다.**

## 26.3 이로써 확정된 재현 조건

지금까지의 배제를 합치면 재현 조건이 하나로 수렴한다:

| 축 | 판정 |
|---|---|
| DAOS 빌드(upstream/exastor/GDS/WSD) | **무관** (§18·§24·§26) |
| 클라이언트 전체(API·캐시·버퍼·프로세스) | **무관** (§15.2) |
| provider / 전송(UCX RDMA·ofi+tcp) | **무관** (§15.5) |
| oclass·복제·chunk 정렬·컨테이너 신선도 | **무관** (§12.4·§13.5) |
| VOS aggregation | **무관** (§12.8) |
| targets/helpers 동시성, 디바이스 수, bdev_roles/메타 위치 | **무관** (§22) |
| 논리 섹터 크기(512 B/4 KiB) | **무관** (§23) |
| server BIO bulk-handle cache | **무관** (§15.3) |
| **실 NVMe 하드웨어 + VFIO/IOMMU DMA 경로(베어메탈)** | **★ 조건** |

즉 이 결함은 **SPDK userspace NVMe 드라이버가 실제 NVMe 컨트롤러(MSI-X 257, 4 KiB)에
VFIO/IOMMU 를 통해 DMA 할 때만** 나타난다. QEMU 가상 NVMe 로는 재현되지 않는다.

## 26.4 남은 실험과 상류 제출 프레임

1. **`class: file`**(sparse file bdev, 우리 베어메탈): SPDK blobstore 로직은 유지하되 실 NVMe
   를 제거. **clean 이면 "실 NVMe DMA 경로" 로 최종 확정**, 손상되면 SPDK blobstore 로직으로.
   → 남은 단 하나의 값싼 분기. 다음 세션 1순위.
2. 다른 모델 실 NVMe(다른 서버)에서 재현 — 하드웨어 일반성(PASCARI 고유인지) 확인.
3. IOMMU 관련: `iommu=pt` 유무, `intel_iommu`/`amd_iommu` 옵션, ATS/PRI 설정 A/B.

**상류 제출 시 반드시 명시할 것**: "VM/가상 NVMe 에서는 재현되지 않고, 베어메탈 실 NVMe +
VFIO 에서만 재현된다. 따라서 상류 CI(대부분 VM)가 이 결함을 잡지 못한다." — 이것이
§14.2 에서 "같은 서명의 보고가 없다"는 사실과 정확히 맞물린다.

---

# 27. ★★★ 최종 확정: `class: file` 은 깨끗 — 조건은 "실 NVMe 를 통한 SPDK DMA" (2026-09-02)

§26.4-1 실행. **같은 베어메탈·같은 서버 빌드·같은 구성(targets 1/helpers 0, 2 디바이스,
`bdev_roles:[wal,meta,data]`, RP_2G1, ofi+tcp)에서 백엔드만 `class: nvme` → `class: file`**
(sparse file bdev, `/var/daos/bdevfiles/nvme{0,1}.img` 60 GB) 로 바꾼 단일변수 실험.

## 27.1 결과 — 실 NVMe 를 빼면 손상이 사라진다

| 백엔드 (모든 조건 동일) | raw object | DFS | 서버 FILLHASH |
|---|---|---|---|
| `class: nvme` (실 PASCARI NVMe ×2, VFIO) | **618/2560 = 24.1 %** | 손상 | **1680** |
| **`class: file` (sparse file ×2, 실 NVMe 미사용)** | **0/2560** | **0/2560** | **0** |

SPDK userspace blobstore·VOS·bio DMA 버퍼 로직은 **그대로 사용**하면서(파일 bdev 도 SPDK
`bdev_aio`/blobstore 경유) 실 NVMe 컨트롤러만 제거했을 때 손상이 **완전히** 사라졌다.

## 27.2 최종 판정

> **손상은 SPDK blobstore/VOS/bio 의 순수 소프트웨어 로직이 아니라, SPDK userspace 드라이버가
> 실제 NVMe 컨트롤러에 VFIO/IOMMU 로 DMA 할 때만 발생한다.**

이로써 §19(채움 단계에서 발생)와 결합해 결함 위치가 최종적으로 좁혀진다:
**`nvme_rw()` → SPDK NVMe 드라이버 → VFIO/IOMMU → 실 NVMe 컨트롤러 DMA** 구간.

배제된 것 총정리(§12~§27): DAOS 빌드 전체, 클라이언트 전체, 전송/provider, oclass·복제,
chunk 정렬, aggregation, bulk-handle cache, targets/helpers, 디바이스 수, bdev_roles·메타
위치, 논리 섹터 크기, **그리고 SPDK blobstore/bio/VOS 소프트웨어 로직(§27)**.

## 27.3 남은 후보 (모두 "실 NVMe DMA" 안쪽)

1. **SPDK NVMe 드라이버의 큐/PRP 처리** — 4 MiB 요청이 PRP 리스트로 쪼개질 때의 경합.
   파일 bdev 는 이 경로를 안 탄다(aio → 커널). 가상 NVMe 는 큐 깊이·MSI-X 규모가 작다.
2. **VFIO/IOMMU DMA 매핑** — IOVA 재사용/무효화 타이밍. §25 에서 UIO 대조는 하드웨어 제약으로
   불가했으므로 이 축은 여전히 미분리.
3. **NVMe 컨트롤러/펌웨어**(PASCARI XX208, MSI-X 257) — 다중 큐 동시 read 에서의 컨트롤러측
   문제. 다른 모델에서의 재현 여부가 이 축을 가른다.

### 다음 세션 우선순위
1. **다른 모델 실 NVMe 로 재현 시도** — 2·3 을 가른다. 재현되면 SPDK/VFIO(범용), 안 되면
   PASCARI 고유(펌웨어/컨트롤러) → 상류 이슈의 성격이 완전히 달라진다.
2. `iommu=pt` 제거/추가, ATS/PRI 토글 A/B (2번 축).
3. SPDK 자체 도구로 우리 NVMe 직접 검증: `spdk_nvme_perf`/`nvme_manage` 로 태그 데이터를
   4 MiB 다중 큐 read 하며 검증 — DAOS 를 완전히 제거한 최소 재현기. **가장 결정적이고
   상류(SPDK) 제출까지 이어질 수 있다.**

## 27.4 상류 제출 프레임 (수정)

DAOS 상류 이슈로는 여전히 유효하나 **성격이 바뀐다**: "DAOS 가 특정 실 NVMe + VFIO 조합에서
fetch 채움 데이터를 조용히 오염시킨다. VM/가상 NVMe·파일 bdev 에서는 재현되지 않아 상류 CI 가
구조적으로 잡을 수 없다." 3번(SPDK 최소 재현기)이 성공하면 **SPDK 프로젝트 이슈**가 더 정확한
제출처가 된다.

## 27.5 환경 상태
현재 **`class: file` arm** 으로 떠 있다(양 rank Joined, pool gdspool 100 GB, 컨테이너
ci_obj·ci_m28, 손상 0). 실 NVMe arm 복귀: `/root/daos_server.yml.nvme2dev`(2 디바이스 24 % arm)
또는 `/root/daos_server.yml.nvme-arm`(8 디바이스 원본) 복원 후 VFIO 재바인딩·재포맷.
34.31 에는 우리 빌드 서버(`daos-ours`)가 여전히 실행중 — 그들 구성 복귀는 `daos-srv4k` 유닛.

---

# 28. ★★★ DAOS 없는 SPDK 최소 재현기 — 깨끗함. 결함은 DAOS 의 SPDK 사용 방식에 있다 (2026-09-02)

§27.3-3 실행. **DAOS 를 완전히 제거하고** SPDK userspace NVMe 드라이버로 같은 디바이스에
직접 태그 I/O 를 하는 최소 프로그램을 작성했다 (`tests/spdk_nvme_tagio.c`).

## 28.1 재현기 설계

- SPDK `spdk_nvme_probe/attach` → 원시 namespace, 큐페어 워커당 1개,
  `spdk_nvme_ns_cmd_write/read` 로 **4 MiB(=DAOS DFS chunk) 단위 I/O**.
- 버퍼는 `spdk_zmalloc(..., SPDK_MALLOC_DMA)` 4 KiB 정렬 — DAOS 의 DMA chunk 와 같은 성격.
- 페이로드는 DAOS 재현기와 같은 자기술 태그 `(region<<48)|(round<<40)|offset`,
  목적지는 0xA5 로 poison, 워커마다 자기 LBA 영역(겹침 없음).
- 빌드(cell1, DAOS 번들 SPDK 빌드 트리 사용) — 링크 조합이 까다로웠다:
  `libspdk_{nvme,env_dpdk,util,log,json,jsonrpc,rpc,sock,trace,vfio_user,keyring,dma,thread}.a`
  + DPDK `librte_{eal,ring,mempool,mbuf,pci,bus_pci,kvargs,telemetry,log,mempool_ring}.a`
  + `-lisal`(crc). **`libspdk_nvmf` 는 넣지 말 것**(bdev/accel 의존을 끌어온다).

## 28.2 결과 — 순수 SPDK 는 손상되지 않는다

| 계층 | 디바이스 | 결과 |
|---|---|---|
| **순수 SPDK**(DAOS 없음), 8 큐 × 20 라운드 | 0000:02:00.0 | 0/160 |
| **순수 SPDK**, 16 큐 × 40 라운드 ×3 | 0000:02:00.0 | **0/1920** |
| **순수 SPDK**, 16 큐 × 40 라운드 | 0000:03:00.0 | **0/640** |
| **DAOS**(같은 두 디바이스, 직후 복원해 측정) | 0000:02·03:00.0 | **172/1280 = 13.4 %** |

sector 4096 정상 인식, I/O 오류 0. 즉 **같은 하드웨어·같은 SPDK 드라이버·같은 VFIO 경로에
같은 크기(4 MiB) 다중 큐 read 를 해도 SPDK 단독으로는 깨끗**하고, 그 위에 DAOS 를 올리면
13 % 가 깨진다.

## 28.3 판정 — §27 의 해석을 정정한다

§27 은 "`class:file` 이 깨끗 ⇒ 실 NVMe DMA 구간의 결함"이라 했다. §28 은 그 결론을 **좁힌다**:

> **실 NVMe + SPDK + VFIO 자체는 무결하다. 결함은 DAOS 가 그 위에서 하는 것 —
> blobstore/bio 가 실 NVMe 경로에서만 드러내는 무언가 — 에 있다.**

두 사실을 함께 놓으면:
- `class:file`(SPDK aio bdev) 깨끗, `class:nvme`(SPDK nvme bdev) 손상 → **bdev 계층 아래
  nvme 전용 경로**가 조건.
- 순수 SPDK nvme 드라이버 직접 사용 깨끗 → **드라이버 자체가 아니라 DAOS 의 사용 방식**.

⇒ 남은 후보가 아주 좁아졌다: **SPDK *blobstore*(`spdk_blob`)가 nvme bdev 위에서 하는
cluster 매핑/IO 분할**, 그리고 그것을 쓰는 **DAOS bio 의 blob I/O 경로**
(`bio_blob_rw`/`nvme_rw` → `spdk_blob_io_read`). 파일 bdev 에서는 같은 blobstore 코드가
깨끗하므로 "blobstore + nvme bdev(4 MiB·다중 큐·큰 cluster)" 조합에 국소화된다.

## 28.4 다음 실험 (개정)

1. **SPDK blobstore 계층 최소 재현기** — `spdk_bs_init/spdk_blob_io_read` 로 blob 을 만들어
   4 MiB 태그 I/O(다중 채널). DAOS 없이 **blobstore 만** 시험한다. 손상되면 **SPDK 프로젝트
   이슈로 확정**(제출처 변경), 깨끗하면 DAOS bio 의 blob 사용 방식으로 최종 확정.
   → `hello_blob` 예제가 이미 빌드 트리에 있어 골격 재활용 가능.
2. `bdev_nvme` 계층(`bdevperf` + 검증 옵션)으로 중간 계층 확인.
3. DAOS 측: `nvme_rw()` 에 요청 LBA/길이/blob 오프셋 로깅을 넣어 손상 chunk 의 blob→LBA
   매핑이 다른 blob 과 겹치는지 직접 확인(§19 계측의 확장).

## 28.5 환경
현재 **실 NVMe 2 디바이스 DAOS arm 복원**(양 rank Joined, pool gdspool 100 GB,
컨테이너 ci_obj, 13 % 재현 확인). SPDK 재현기는 cell1 `/tmp/spdk_nvme_tagio`(소스는 레포
`tests/spdk_nvme_tagio.c`). SPDK 단독 실행 시 DAOS 를 정지해야 한다(디바이스 배타 점유).

---

# 29. ★★★ blobstore 계층도 깨끗 — SPDK 전 계층 무죄, 결함은 DAOS 의 blob 사용 (2026-09-02)

§28.4-1 실행. **DAOS 를 제거하고 SPDK blobstore 만** 시험하는 재현기를 작성
(`tests/spdk_blob_tagio.c`, 빌드 `tests/spdk_blob_tagio.build.sh`).

## 29.1 재현기 설계 (DAOS bio 형태 모방)

- `spdk_bs_init()` on **nvme bdev**(`bdev_nvme_attach_controller` JSON) — 즉 §27 에서
  손상이 나던 그 조합(blobstore + nvme bdev)을 DAOS 없이 재현.
- 워커당 blob 1개(=VOS blob per target), 워커당 `spdk_bs_alloc_io_channel()` 1개,
  `spdk_blob_io_write/read` 로 **4 MiB(=DFS chunk) I/O**, 전 워커 동시 진행.
- `spdk_zmalloc(SPDK_MALLOC_DMA)` 4 KiB 정렬 버퍼, 0xA5 poison, 같은 자기술 태그.
- SPDK app framework(단일 reactor, 비동기 상태기계) 사용 — blobstore 호출은 SPDK 스레드에서만.

빌드/실행 함정 4건: ① `-R` 은 SPDK 예약 옵션 → 라운드 플래그를 `-N` 으로.
② `spdk_bs_unload()` 는 blob 이 열려 있으면 거부 → teardown 에서 blob 을 순차 close 하고,
**판정 출력은 unload 전에** 한다(teardown 실패가 측정을 가리지 않게). ③ accel 이
isal_crypto·lz4 를 요구 → `-lisal_crypto -llz4` 추가. ④ 비정상 종료 시
`/var/tmp/spdk_cpu_lock_*` 이 남아 다음 실행이 코어 락 실패 → 삭제 필요.

## 29.2 결과 — 깨끗

```
blobstore: page=4096 cluster=1048576 free_clusters=3648520 | workers=16 rounds=40 io=4MiB
PASS: 0/640   PASS: 0/640
```

| 계층 (같은 디바이스 0000:02:00.0) | 결과 |
|---|---|
| DAOS (class:nvme, 실 NVMe) | **13~24 % 손상** (§27·§28) |
| **SPDK blobstore + nvme bdev** (DAOS 없음), 16 blob × 40 라운드 ×2 | **0/1280** |
| SPDK NVMe 드라이버 직접 (§28) | 0/2560 |
| DAOS class:file (blobstore + aio bdev) (§27) | 0/2560 |

## 29.3 판정 — SPDK 전 계층 무죄

> **NVMe 드라이버·bdev_nvme·blobstore 모두 무결하다. 4 MiB 다중 blob·다중 채널 동시
> read 를 같은 하드웨어에서 해도 깨끗하다. 결함은 DAOS 가 blobstore 를 쓰는 방식에 있다.**

이제 §19(손상은 `bio_iod_prep()` 반환 시점에 이미 존재 = NVMe→DMA 채움 구간)와 결합하면
용의 코드가 **DAOS bio 의 blob I/O 경로**로 확정된다:

```
nvme_rw()            src/bio/bio_buffer.c   — 영역별 blob I/O 발행
 └ bio_blob_rw()/spdk_blob_io_read(ov)
    - blob 오프셋 계산: bio_iov 의 ba_off → blob page/cluster
    - SGL 경로: spdk_blob_io_readv 사용 시 iov 배열 구성
    - 채널: 엔진 xstream 당 blob io_channel 공유
```

**§27 의 class:file 이 깨끗한 이유도 여기서 설명된다**: 같은 DAOS 코드라도 aio bdev 는
동기적 완료·단일 큐라 경합 창이 없고, nvme bdev 는 다중 큐/비동기라 DAOS 측 계산·수명 오류가
드러난다. 즉 **DAOS 버그이며, nvme bdev 에서만 노출된다.**

## 29.4 다음 실험 (최종 좁히기)

1. **`nvme_rw()` 계측**: 발행하는 (blob, blob_offset, length, buffer_addr) 를 로깅하고,
   §19 의 FILLHASH 가 잡은 손상 chunk 의 요청 파라미터가 **다른 blob 의 영역과 겹치는지**
   직접 확인. 겹치면 DAOS 의 오프셋 계산 오류로 **원인 확정**.
2. `spdk_blob_io_readv`(SGL) 대 `spdk_blob_io_read`(단일 버퍼) 경로 확인 — DAOS 가 어느 쪽을
   쓰는지, iov 구성에 오류가 없는지.
3. 확정 후 상류 제출: **DAOS 이슈**(SPDK 아님)로, 재현기 3종(DAOS 레벨·SPDK blobstore 음성
   대조·SPDK 드라이버 음성 대조)을 함께 첨부하면 "우리 계층 아님"을 선제적으로 차단할 수 있다.

## 29.5 환경
DAOS 는 정지 상태이고 0000:02:00.0 은 SPDK(vfio)에 바인딩돼 blobstore 재현기가 그 위에
blobstore 를 새로 만들었다(**디바이스 내용 파기됨 — DAOS 재사용 시 재포맷 필요**).
DAOS arm 복귀: `/root/daos_server.yml.nvme2dev` 유지 상태이므로 서버 기동 → 재포맷 → pool·컨테이너
재생성. 재현기: cell1 `/tmp/spdk_blob_tagio`(+`/tmp/blob_nvme.json`), `/tmp/spdk_nvme_tagio`.

---

# 30. `nvme_rw()` blob I/O 계측 — 요청은 정상, 데이터만 틀리다 (2026-09-02)

§29.4-1 실행. stock 트리 `nvme_rw()` 에 발행 파라미터 로깅을 추가
(`BLOBIO_DEBUG=1` gate, `BLOBIO R/W blob= ch= payload= io_off= io_cnt=`).
FILLHASH(§19)와 같은 엔진 로그에 남으므로 손상 시점과 직접 대조된다.

## 30.1 수집

한 런(16×15, 클라 24/240 손상)에서: **4 MiB blob read 887건**, FILLHASH 2건.
손상 직전 6건의 4 MiB 읽기:

```
blob=0x7fd604575c60 ch=0x7fd5d839eba0 payload=0x203020200000 io_off=12994487 io_cnt=1024
blob=0x7fd604575c60 ch=0x7fd5d839eba0 payload=0x203020200000 io_off=13038519 io_cnt=1024
blob=0x7fd604575c60 ch=0x7fd5d839eba0 payload=0x203015400000 io_off=13035447 io_cnt=1024
...
FILLHASH ... exp_tid=4 ... foreign=524288, first bad at 0 val t3 r7 off 8388608
```

## 30.2 관측 — 그리고 배제

1. **DMA 버퍼 재사용은 원인이 아니다.** 같은 payload 주소가 서로 다른 io_off 에 재사용되는
   패턴이 **887건 중 882건**(정상 풀 동작). 손상은 24건뿐이므로 재사용 자체와 상관없다.
   최대 61개의 서로 다른 io_off 가 한 payload 주소를 공유한다 — 정상.
2. **모든 요청이 같은 blob·같은 채널**(`blob=0x...c60`, `ch=0x...eba0`). 즉 DAOS 는 타깃
   xstream 의 단일 blob/채널로 읽고 있고, **요청 파라미터에 다른 blob 이 섞이지 않는다.**
3. **손상 데이터는 "다른 객체의 다른 오프셋"**: FILLHASH 는 t4 의 버퍼에 `t3 r7 off 8388608`
   데이터가 들어왔다고 말한다. 클라이언트 기록도 같은 형태(`t11` chunk0 ← `t0 r0 off 0`,
   `t12` ← `t0 r1 off 4194304`).

## 30.3 판정 — 남은 두 갈래

DAOS 는 **정상적인 blob 오프셋으로 요청**하는데 **다른 데이터가 채워진다**. 요청 파라미터
오류(§29.3 의 1순위 가설)는 **기각**된다. 남은 것:

| 갈래 | 내용 | 다음 확인 |
|---|---|---|
| **A. blob→LBA 매핑** | DAOS 요청 io_off 는 맞지만 그 blob 의 cluster 매핑이 다른 객체 데이터를 가리킴(= blob 할당/확장 시점의 문제) | 손상 chunk 의 io_off 를 `spdk_blob_get_clusters`/ddb 로 LBA 로 환산해 다른 blob 과 겹치는지 |
| **B. 완료 매칭** | 요청은 맞고 데이터도 맞게 읽혔으나 **완료 콜백이 다른 요청의 버퍼에 귀속**(rw_completion ↔ biod 연결) | 요청마다 고유 태그를 심어 완료 시 대조(다음 계측) |

§29 에서 **동일 shape 의 blobstore 단독 시험이 깨끗**했음을 감안하면 B(완료/버퍼 귀속)가 더
유력하다 — blobstore 자체는 요청↔완료를 정확히 처리했기 때문이다. DAOS 측에서 그 매칭을
어긋나게 하는 것은 `bio_desc`(biod) 재사용·`bd_inflights` 회계·`drain_inflight_ios()` 의
동시성이다.

## 30.4 다음 계측 (원인 확정용)

`rw_completion()` 에 **완료된 요청의 (blob, io_off, payload)** 를 로깅하고 발행 로그와
1:1 대조한다. 발행/완료 쌍이 어긋나면 **B 확정**(DAOS 의 요청-완료 귀속 버그). 일치하면
**A**로 넘어가 blob cluster 매핑을 덤프한다.

현재 계측 상태: cell1/cell2 `/var/daos-stockfull` = upstream + FILLHASH + BLOBIO 로깅.
소스는 cell1 `/var/daosbuild/daos-stock`(원본 백업 `/tmp/bio_buffer.c.pre-bloblog`,
`/tmp/srv_obj.c.orig`). 로그 폭증 주의 — 4 MiB 읽기당 1줄.

## 30.5 완료 계측 결과 — 갈래 B(완료 귀속)도 기각

`rw_completion()` 에도 로깅 추가(`BLOBIO DONE biod= err= inflights_left= type=`).
발행 측에는 thread-local 시퀀스 번호를 붙였다. (빌드 함정: `rw_completion()` 이 헬퍼
정의보다 앞서므로 **파일 스코프 전방 선언 필수** — 함수 본문 안에 넣으면
`invalid storage class` 로 실패한다.)

손상 50/192 를 유도한 런에서:

| 지표 | 값 |
|---|---|
| 4 MiB read 발행 | 817 |
| 완료 콜백 | 3616 (대부분 WAL/체크포인트의 `io_cnt=1` 소형 I/O) |
| `inflights_left=0` 비율 | 4807/4823 |
| 한 `biod` 주소당 발행/완료 | 24 / 176 — **주소 재사용** |

시간순 추적 결과 **발행과 완료가 정확히 1:1 로 교대**한다(`W` → `DONE inflights_left=0`).
`biod` 주소가 176회 등장하는 것은 **구조체 풀의 순차 재사용**이지 귀속 오류가 아니다.

⇒ **§30.3 의 갈래 B(완료가 다른 요청의 버퍼에 귀속) 기각.** 요청도 정상, 완료 매칭도 정상,
그런데 버퍼 안의 데이터만 다른 객체 것이다.

### 남은 것은 갈래 A — blob→LBA 매핑

DAOS 가 올바른 blob 오프셋으로 요청하고 완료도 올바르게 매칭되는데 내용이 다른 객체의 것이라면,
**그 blob 오프셋이 물리적으로 다른 객체의 데이터를 가리키고 있다**는 뜻이다. 즉 blob 의
cluster 할당/매핑이 어긋나 있다. 이는 §29 의 blobstore 단독 시험이 깨끗했던 것과도 모순되지
않는다 — 그 시험은 **blob 을 새로 만들어 한 번만 쓴** 반면, DAOS 는 **VOS 가 blob 안에서
공간을 할당·재사용**(VEA)하기 때문이다.

⇒ **최종 용의자: VEA(=DAOS 의 blob 내부 공간 할당자)가 서로 다른 객체에 겹치는 extent 를
내주는 것.** §17.4 의 S2 가설로 되돌아왔고, 이제 다른 모든 갈래가 배제되어 단독으로 남았다.

### 다음 (원인 확정)
1. `vea_reserve()`/`vea_free()` 에 (blk_off, blk_cnt, 소유 객체) 로깅 → 손상 chunk 의
   io_off 를 blk_off 로 환산해 **다른 객체의 예약과 겹치는지** 직접 확인. 겹치면 **확정**.
2. `ddb` 로 손상 후 VOS 트리를 덤프해 두 객체의 extent 가 같은 blk 를 가리키는지 확인(정적 증거).
3. 확정되면 상류 이슈는 **VEA 할당자 버그**로, 재현기 4종(§28·§29 음성 대조 포함) 첨부.

---

# 31. VEA 계측 — 할당자도 결백. 모든 계층이 "정상"인데 데이터만 틀리다 (2026-09-02)

§30.5 가 남긴 마지막 갈래(VEA 가 겹치는 extent 를 내준다)를 계측했다.
`vea_reserve()`/`vea_free()` 에 로깅 + **라이브 예약 테이블 기반 겹침 즉시 판정**
(`VEALOG_DEBUG=1`, `tests` 외부: cell1 `/var/daosbuild/daos-stock/src/vea/vea_api.c`,
원본 `/tmp/vea_api.c.pre-log`).

## 31.1 결과 (손상 25/192 유도 런)

| 지표 | 값 |
|---|---|
| `VEALOG OVERLAP` (겹치는 예약) | **0** |
| `VEALOG RESERVE` | 1344 (**전부 `cnt=1024`=4 MiB, offset 전부 고유**) |
| `VEALOG FREE` | **0** (런 중 재사용 없음) |
| 4 MiB blob read 발행 | 731 |
| FILLHASH(채움 직후 손상) | 18 |
| **읽기 io_off 가 자기 예약 범위 안에 있는 비율** | **731 / 731 (100 %)** |

VEA blk 와 blob io unit 이 모두 4096 B 라 직접 비교했다. **모든 읽기가 유효한 예약 안이고,
어떤 두 예약도 겹치지 않는다.**

## 31.2 판정 — 갈래 A 도 기각, 모순 상태에 도달

§30 과 합치면 지금까지 확인된 것은:

| 단계 | 상태 |
|---|---|
| VEA 예약 | 겹침 없음, 전부 고유 (§31.1) |
| DAOS 가 요청하는 blob 오프셋 | 자기 예약 안, 정상 (§31.1) |
| blob I/O 요청 파라미터(blob·채널·범위) | 정상 (§30.2) |
| SPDK blobstore/bdev/드라이버 | 무결 (§27·§28·§29) |
| 완료 콜백 귀속 | 1:1 정상 (§30.5) |
| **채움 직후 버퍼 내용** | **다른 객체 데이터 (§19·§31.1)** |

즉 **"올바른 blob 의 올바른 오프셋을, 겹치지 않는 영역에서, 올바르게 완료된 요청으로 읽었는데
다른 객체의 데이터가 들어 있다."** 남은 가능성은 좁고 구체적이다:

1. **쓰기 측 오배치** — 손상은 read 가 아니라 **write** 에서 발생했을 수 있다. 지금까지의 계측은
   전부 read 경로였다. 객체 A 의 데이터가 객체 B 의 blk 에 기록됐다면, B 를 정확히 읽어도 A 의
   데이터가 나온다. **§19 의 "at-rest 는 대체로 정상"과 상충하는 듯하지만, at-rest 검증은
   손상 이후 재기록된 상태를 본 것일 수 있다.**
2. **VOS extent → blob offset 변환** — VEA 예약은 정상이나 `vos_io` 가 biov 에 채워 넣는
   `ba_off` 가 다른 객체의 예약을 가리킬 수 있다(예약 자체는 고유해도 **매핑 단계**에서 뒤바뀜).
3. SPDK blobstore 의 **cluster 매핑**(blob 오프셋→LBA)이 두 blob 에서 겹침 — §29 의 단독
   시험은 blob 을 한 번만 확장했으므로 이 경로를 충분히 흔들지 못했을 수 있다.

## 31.3 다음 (가장 값싼 순)

1. **쓰기 경로 FILLHASH** — `bio_iod_post()`(update 완료) 직전에 DMA 버퍼를 감사해, 기록되는
   내용이 그 객체 것인지 확인. 손상이 쓰기 측이면 여기서 잡힌다. **1순위**: read 측 계측이
   전부 "정상"인 지금, 가장 큰 미탐색 영역이다.
2. **`ba_off` 로깅** — `nvme_rw()` 에서 `rg->brr_off`(=blob offset)와 그 IOD 의 객체(OID)를
   함께 남겨, **같은 blob offset 을 서로 다른 OID 가 읽는지** 직접 확인. 겹치면 §31.2-2 확정.
3. `spdk_blob_get_clusters()` 로 손상 시점 두 blob 의 cluster 맵 덤프(§31.2-3).

## 31.4 현재 계측 상태
cell1/cell2 `/var/daos-stockfull` = upstream + FILLHASH(srv_obj) + BLOBIO(bio_buffer) +
VEALOG(vea_api). 원본 백업: `/tmp/srv_obj.c.orig`, `/tmp/bio_buffer.c.pre-bloblog`,
`/tmp/bio_buffer.c.pre-compl`, `/tmp/vea_api.c.pre-log`.
로그량 주의: VEALOG 는 예약당 1줄, BLOBIO 는 I/O 당 1줄.

---

# 32. 쓰기 경로 계측 — 페이로드 쓰기도 결백 (2026-09-02)

§31.3-1 실행. `bio_iod_post()` 의 `dma_rw()`(=미디어로 내려보내기) **직전**에 DMA 버퍼를
감사(`WRAUDIT_DEBUG=1`). 재현기 페이로드는 워드마다 `(tid,round,offset)` 태그를 가지므로,
내려갈 버퍼는 **하나의 tid·하나의 round** 여야 한다.

## 32.1 결과 (손상 63/192 유도 런)

| 항목 | 값 |
|---|---|
| **4 MiB 페이로드 쓰기 `WRAUDIT OK`** | **1344** |
| **4 MiB 페이로드 쓰기 `WRAUDIT MIXED`** | **0** |
| 작은 버퍼(4 KiB~256 KiB) MIXED | 52 |
| FILLHASH(읽기 채움 손상) | 104 |

52건의 MIXED 는 전부 **len 4096~262144 의 소형 버퍼**이고 `tid0=0 round0=0` 이다 — VOS
메타데이터/WAL 버퍼가 우연히 태그 형태로 해석된 **오탐**(감사기가 "태그처럼 보이는" 버퍼만
검사하므로 소형 메타 버퍼를 걸러내지 못했다). 실제 객체 페이로드인 **4 MiB 쓰기는 1344건
전부 무결**하다.

## 32.2 판정 — 쓰기도 결백, 모순이 굳어졌다

> **디스크로 내려가는 쓰기 버퍼는 정확하다.** 따라서 "A 의 데이터가 B 의 blk 에 기록된다"는
> §31.2-1 가설은 **기각**이다.

이로써 전 구간이 개별적으로는 정상으로 측정됐다:

| 구간 | 상태 | 근거 |
|---|---|---|
| 쓰기 버퍼(미디어 직전) | 정상 | §32.1 |
| VEA 예약(겹침/범위) | 정상 | §31.1 |
| 읽기 요청 파라미터 | 정상 | §30.2 |
| 완료 콜백 귀속 | 정상 | §30.5 |
| SPDK 드라이버/bdev/blobstore | 무결 | §27·§28·§29 |
| **읽기 채움 직후 버퍼** | **오염** | §19·§31 |

## 32.3 남은 가능성 (좁고 구체적)

1. **쓰기 버퍼는 맞지만 착지 위치가 틀리다.** WRAUDIT 는 *내용*만 봤고 *목적지 blk* 는
   기록만 했다. `blk_off` 를 그 IOD 의 객체와 함께 남겨 **두 객체가 같은 blk 에 쓰는지**
   확인해야 한다(§31 은 VEA *예약*만 봤지, biov 가 실제로 그 예약을 쓰는지는 안 봤다).
   ⇒ **1순위**: `nvme_rw()` 에서 write 시 `(OID, blk_off, len)` 로깅 후 중복 검사.
2. **읽기 시 blob→LBA 변환이 다른 예약을 가리킨다** — §31.1 은 blob 오프셋이 예약 안에
   있음을 봤지만, 그 예약이 **그 객체의 것인지**는 확인하지 않았다(예약 소유자 미기록).
   ⇒ 예약 시 OID 를 함께 기록하면 1·2 를 동시에 판정할 수 있다.
3. SPDK blobstore cluster 맵이 두 blob 에서 겹침(§31.2-3, 여전히 미확인).

**1·2 는 같은 계측으로 해결된다: "누가 어느 blk 를 쓰고 읽는가" 를 OID 와 함께 기록하는 것.**
지금까지의 계측은 각 계층을 따로 봤을 뿐, **소유권**을 한 번도 추적하지 않았다 — 그것이 남은
구멍이다.

## 32.4 다음 계측 (설계)
`bio_iov` 에서 `bio_iov2raw_off()` 로 blk 를 얻고, `biod` 에서 상위 OID 를 얻어
(`bio_desc` 에는 OID 가 없으므로 `vos_io` 계층 또는 `srv_obj` 에서 내려줘야 한다)
`OWNER blk=... oid=... op=R/W` 를 남긴 뒤, 같은 blk 를 서로 다른 oid 가 만지는지 집계한다.
FILLHASH 가 이미 srv_obj 에서 OID 를 갖고 있으므로, 그 지점에서 IOD 의 biov 목록을 함께
덤프하는 것이 가장 값싸다.

---

# 33. ★ 소유권 계측 — 이것도 정상. DAOS 계층 추적의 종착점 (2026-09-02)

§32.4 설계대로, FILLHASH 가 이미 OID 를 갖고 있는 `srv_obj` 지점에서 IOD 의 biov 를 덤프해
**어느 객체가 어느 미디어 블록을 만지는지**(`OWNER blk=<off>+<cnt> op=R|W oid=<OID>`)를
읽기·쓰기 양쪽에서 기록했다.

## 33.1 결과 (손상 13/160 유도 런)

| 검사 | 결과 |
|---|---|
| OWNER 항목 | 1704 |
| **두 개 이상의 OID 가 만진 블록** | **0** |
| 쓰인 블록 / 읽힌 블록 | 1,146,880 / 552,960 |
| **읽었지만 이 구간에서 쓰인 적 없는 블록** | **0** |
| **읽은 블록의 writer OID ≠ reader OID** | **0** |

⇒ **모든 읽기는 자기 객체가 쓴 바로 그 블록을 읽는다.** 소유권 혼선은 없다.

## 33.2 DAOS 계층 추적의 종착점

§19 부터 §33 까지, 손상 경로의 **모든 단계를 개별 계측으로 확인**했고 전부 정상이다:

| 단계 | 계측 | 결과 |
|---|---|---|
| 쓰기 버퍼(미디어 직전) | WRAUDIT | 4 MiB 쓰기 1344/1344 정상 |
| 객체→블록 소유권(쓰기) | OWNER W | 중복 0 |
| VEA 예약 | VEALOG | 겹침 0, 전부 고유 |
| 객체→블록 소유권(읽기) | OWNER R | writer≠reader 0 |
| 읽기 요청 파라미터 | BLOBIO R | blob·채널·범위 정상, 예약 내 100 % |
| 완료 콜백 귀속 | BLOBIO DONE | 발행:완료 1:1 |
| SPDK 드라이버/bdev/blobstore | 단독 재현기 | 무결(0/2560, 0/1280) |
| **채움 직후 버퍼 내용** | FILLHASH | **다른 객체 데이터** |

**모든 메타데이터·제어 경로가 정확한데 데이터만 틀리다.** 이는 "DAOS 가 잘못된 것을
요청한다"는 모든 가설의 부정이며, 남은 해석은 두 가지뿐이다:

1. **DAOS 가 옳게 요청한 I/O 에 대해 장치/스택이 잘못된 데이터를 반환한다** — 단, §28·§29 의
   단독 재현기가 같은 장치·같은 크기·같은 다중 큐에서 깨끗했으므로, **DAOS 가 만드는 특정
   I/O 패턴에서만** 그렇다는 뜻이 된다(예: 동시 read/write 혼합, 특정 큐 깊이, 특정 시점의
   blob 확장 등 — 단독 재현기가 재현하지 못한 조건).
2. 계측 자체가 놓치는 경로가 있다 — 예컨대 **checkpoint/aggregation 등 백그라운드 I/O**
   (OWNER/WRAUDIT 는 클라이언트 IOD 경로만 본다)가 같은 블록을 건드리는 경우.

**2 번이 유력하다.** §12.5 에서 이미 aggregation 이 자체 checksum 검증에 실패하는 것을 봤고
(그때는 "원인 아님"으로 §12.8 에서 기각됐으나 그건 *발생률* 기준 판정이었다), MD-on-SSD 의
**checkpoint** 는 WAL→data blob 으로 데이터를 옮기는 별도 경로다. 지금까지의 계측은
**이 경로를 한 번도 보지 않았다.**

## 33.3 다음 (남은 유일한 미계측 경로)
1. **checkpoint/WAL 경로 계측** — `bio_wal_*`/checkpoint 가 data blob 에 쓰는 (blk, 내용)을
   WRAUDIT 와 같은 방식으로 감사. 손상 블록이 checkpoint 대상과 겹치는지 확인. **1순위.**
2. `DAOS_IO_BYPASS=wal_commit`(§17.3 에서 확인한 기존 노브)으로 WAL 을 끄고 A/B —
   구성 변경만으로 checkpoint 축을 시험할 수 있다. **값이 가장 싸다.**
3. aggregation 완전 정지(`reclaim:disabled`, §12.8 에서 이미 인프라 존재) + checkpoint 만 남기는
   조합으로 축 분리.

**2 번을 먼저 하는 것이 합리적이다** — 코드 수정 없이 checkpoint/WAL 축을 즉시 시험한다.

---

# 34. ★★ 손상은 "포맷 직후 첫 런"에서만 일어난다 (2026-09-02)

§33.3 의 WAL/checkpoint 축을 시험하려다, **그보다 훨씬 큰 것**을 발견했다.
이 절은 지금까지의 모든 A/B 를 다시 읽게 만든다.

## 34.1 `DAOS_IO_BYPASS=wal_commit` 은 쓸 수 없다 (무효)

가장 싼 수로 제안했던 노브는 **엔진을 죽인다**. arm B 로 부팅한 뒤 pool create 하면:

```
EMRG src/common/dav_v2/heap.c:205 heap_zinfo_init()
     Assertion 'z0->header.zone0_zinfo_size == alloc_size' failed
*** received signal 6 (Aborted) *** / signal 11 (Segmentation fault)
dmg: pool create failed: dRPC recv chunk 0: EOF
```

첫 크로스오버(A B B A B A A B)에서 **B 4런이 전부 빈 결과**였는데, 이는 면역이 아니라
**풀 생성 자체가 실패**한 것이었다. 출력을 버렸다면 "wal_commit 을 끄면 손상이 사라진다"는
완전히 틀린 결론을 냈을 것이다. 이 축은 이 노브로는 시험 불가.

## 34.2 대신 발견한 것 — 포맷 경계

checkpoint 는 **풀 속성**(`checkpoint:disabled|timed|lazy`)이라 재기동 없이 A/B 가 된다.
그렇게 8런을 돌렸더니 **양쪽 arm 모두 0/160, 총 1280 검사에서 손상 0건**이었다.
그런데 같은 날 오전 wal_ab 의 arm A 는 8·3·3·27 /160 이었다. 두 하네스의 구조적 차이는
단 하나 — **런마다 재기동+포맷을 했는가**.

직접 갈랐다. 포맷 1회 후 같은 부팅에서 3런 연속(런마다 풀은 새로 생성):

| | 결과 |
|---|---|
| **포맷 직후 1런** | **FAIL 58/160 (36.3 %)** |
| 같은 부팅 2런 | PASS 0/160 |
| 같은 부팅 3런 | PASS 0/160 |

누적하면:

| 조건 | 손상 / 검사 | 비율 |
|---|---|---|
| **포맷 직후 첫 런** (wal_ab A 4런 + 위 1런) | **99 / 800** | **12.4 %** |
| 첫 런 이후 (ck_ab 8 + ck_ab2 8 + 위 2런) | 1 / 2880 | 0.03 % |

**약 350 배.** 손상은 산발적 확률 사건이 아니라 **포맷 경계에 묶인 결정적 현상**이다.

## 34.3 이것이 뒤집는 것

1. **재현 레시피가 바뀐다.** "동시 읽기를 오래 돌린다"가 아니라 **"포맷하고 한 번 돌린다"**
   이다. 업스트림 보고서의 재현 절차를 이 형태로 다시 써야 한다.
2. **과거 A/B 의 재해석이 필요하다.** 런마다 포맷한 하네스는 모든 arm 이 "첫 런"이라
   비교 자체는 공정했다. 그러나 포맷 없이 반복한 측정은 **신호가 없는 구간을 측정**한 것이라
   "이 축은 무관"이라는 판정의 검정력이 사실상 0 이었을 수 있다. §12·§20·§21 계열 중
   포맷 경계를 통제하지 않은 것은 재검토 대상이다.
3. **§33 의 소유권 계측이 왜 깨끗했는지 설명될 수 있다.** 그 런은 13/160 으로 손상이 있었고
   OWNER 는 깨끗했다 — 그렇다면 남는 해석은 **클라이언트 IOD 경로 바깥**, 즉 포맷 직후에만
   활성인 무언가다.

## 34.4 포맷 직후에만 다른 것은 무엇인가

- **blobstore/VEA 가 한 번도 쓰이지 않은 클러스터를 처음 할당한다.** 두 번째 런부터는
  이전 런이 이미 기록한 영역을 재사용한다.
- **NVMe 의 미기록(deallocated) 블록.** 포맷 후 처음 읽는 LBA 는 장치가 임의 데이터를
  돌려줄 수 있는 유일한 구간이다 — 단 §33 은 읽기 전에 쓰기가 있었음을 보였으므로,
  쓰기와 읽기 사이의 **경합**이 아니면 설명되지 않는다.
- **WAL 이 비어 있는 상태에서의 첫 checkpoint.** 첫 런에서만 WAL→data blob 첫 이동이 일어난다.

세 번째가 §33.3 의 가설과 정확히 맞물린다. 그래서 checkpoint A/B 를 **런마다 포맷**하도록
고쳐 재실행 중이다(§35).

## 34.5 방법론 교훈 (세 번째 반복)

이 조사에서 잘못된 arm 비교가 나온 것이 이번이 세 번째다(§20 발생률, §22 kdev, 그리고 이번).
매번 원인이 같았다: **arm 이 실제로 의도한 조건이었는지 검증하지 않았다.** 이번에 추가한 가드 —
풀 속성을 되읽어 요청값과 다르면 그 런을 채점하지 않고 `ABORT` 로 기록 — 는 두 번째 하네스에서
즉시 값을 했다. `--force` 가 아니라 `--recursive` 였던 탓에 destroy 가 조용히 실패했고,
8런이 전부 1런의 풀을 재사용하고 있었다. 가드가 없었다면 이것도 데이터로 둔갑했을 것이다.

**검증되지 않은 런은 0 으로 채점하지 말고 버려야 한다.**

---

# 35. checkpoint 축 — 복제 실패. 확정하지 않는다 (2026-09-02)

§34.3 의 checkpoint 크로스오버를 순서를 뒤집어(B A A B A B B A) 복제했다.

| | A `timed` (기본) | B `disabled` | 평균차 | 정확 순열 p |
|---|---|---|---|---|
| 복제 1 (A B B A B A A B) | 47, 24, 23, 28 → **19.06 %** | 20, 19, 15, 9 → **9.84 %** | +14.75 | **0.029** |
| 복제 2 (순서 반전) | 17, 56, 18, 10 → 15.78 % | 22, 7, 31, 14 → 11.56 % | +6.75 | **0.714** |
| **합산 (arm 당 8런)** | 223/1280 = 17.42 % | 137/1280 = 10.70 % | +10.75 | **0.114** |

**복제 실패다.** 복제 1 의 완전 분리(p=0.029, n=4 대 n=4 의 최소값)는 재현되지 않았고,
합산 p = 0.114 는 어떤 기준으로도 통과하지 못한다.

## 35.1 판정

**checkpoint 축은 입증되지 않았다.** §33.3 에서 "1순위"로 지목했던 가설은 현재 근거로
지지되지 않는다. 이 조사에서 arm 비교가 뒤집힌 **네 번째** 사례다(§20 발생률, §22 kdev,
§34.1 wal_commit, 그리고 이번).

단, 두 복제 모두 **방향은 A > B 로 일치**한다(19.1>9.8, 15.8>11.6). 효과가 없다는 증명도
아니다 — 런 단위 분산이 워낙 커서(A 는 10~56, B 는 7~31) 검정력이 부족할 뿐이다.
관측 효과크기(+10.75/런, 런 SD ≈ 15)를 80 % 검정력으로 잡으려면 **arm 당 약 34 런**,
즉 3~4 시간이 필요하다. 그 비용을 쓸 가치가 있는지는 별개 판단이다.

## 35.2 부수 소득 — §34 는 오히려 강해졌다

두 복제는 런마다 포맷하는 구조라 **16 런 전부가 "포맷 직후 첫 런"** 이었다.
그리고 **16 런 전부 손상이 났다**(최소 7, 최대 56, 합 360/2560 = 14.1 %).

포맷 경계 통계를 갱신하면:

| 조건 | 손상 런 / 전체 런 | 손상 / 검사 | 비율 |
|---|---|---|---|
| **포맷 직후 첫 런** | **21 / 21** | 459 / 3360 | **13.7 %** |
| 첫 런 이후 | 1 / 18 | 1 / 2880 | 0.03 % |

**21런 전부 손상, 18런 중 1런.** §34 의 발견은 이제 이 조사에서 가장 견고한 사실이며,
동시에 **가장 유용한 재현 레시피**다.

## 35.3 다음
1. **포맷 직후에만 다른 것을 직접 계측한다.** §34.4 의 세 후보 중 checkpoint 는 약해졌으니,
   남은 둘 — 처음 할당되는 blobstore 클러스터, 미기록 NVMe LBA — 로 좁힌다. 구체적으로는
   **첫 런에서 읽히는 블록이 그 런에서 처음 할당된 클러스터인지**를 VEA/blob 할당 시각과
   함께 기록한다. §33 의 OWNER 계측에 "이 blk 가 이번 포맷 이후 처음 쓰인 것인가"를 더하면 된다.
2. 업스트림 티켓은 **§34 레시피로 지금 쓸 수 있다** — 21/21 은 어떤 리포트에도 충분하다.
   checkpoint 는 언급하되 미확정으로 남긴다.

---

# 36. 첫-쓰기 계측 — 축은 무의미했으나, 배제 하나와 결정적 단서 하나 (2026-09-02)

§35.3 대로 OWNER 계측에 **블록별 쓰기 이력**을 추가했다. 엔진은 포맷 때 재시작하므로
"엔진 기동 이후"는 곧 "포맷 이후"다. 4 KiB 블록마다 포화 카운터 1 바이트를 두고,
쓰기 때 증가시키며 읽기 때 되묻는다(`OWNER ... firstw= never= once=`,
`FILLHASH blk=..+.. never= once=`).

## 36.1 첫 런 안에서는 대조가 성립하지 않는다

손상 36/160 이 난 런에서:

| | |
|---|---|
| 쓰인 블록 1,146,880 중 **포맷 이후 첫 쓰기** | 1,146,880 (**100 %**) |
| 읽힌 블록 677,888 중 **한 번만 쓰인 것** | 677,888 (**100 %**) |

첫 런에서는 정의상 모든 것이 새것이라 **손상/정상을 가르지 못한다.** 이 축의 대조는
런 사이에만 존재하고, 그건 §34 가 이미 측정한 것이다.

## 36.2 배제 — "미기록 LBA 를 읽어서"가 아니다

손상 영역 38 개, 각 1024 블록(= 4 MiB, 청크 크기와 일치):

| | |
|---|---|
| 손상 영역 중 **한 번도 쓰인 적 없는 블록** | **0 (0.00 %)** |
| 손상 영역 중 정확히 한 번 쓰인 블록 | 38,912 (**100 %**) |

§34.4 의 후보 두 번째 — **미기록 NVMe LBA 가 임의 데이터를 돌려준다** — 는 **배제된다.**
손상된 읽기는 예외 없이 *이미 기록된* 블록을 읽는다.

## 36.3 ★ 결정적 단서 — 쓴 놈과 읽는 놈이 같은데 내용은 남의 것

손상 블록을 OWNER 기록과 교차 조회한 결과:

| | |
|---|---|
| 손상 영역 중 **쓴 OID == 읽는 OID** | **38 / 38 (전부)** |
| 쓴 OID 가 모든 읽는 OID 와 다름 | 0 |
| 쓴 기록이 없음 | 0 |

그런데 그 블록의 **내용은 다른 객체의 것**이다:

```
oid=...3237998095.0.2  expected tid=15  payload says tid=3  round=1
oid=...3237998088.0.2  expected tid=8   payload says tid=2  round=2
oid=...3237998088.0.2  expected tid=8   payload says tid=13 round=2
oid=...3237998082.1.2  expected tid=2   payload says tid=7  round=2
```

같은 객체가 같은 블록을 두 번 읽어 **매번 다른 남의 tid** 를 받는 경우까지 있다(tid 2, 13).

**정리하면: X 가 blk B 에 썼고, X 만 B 에 썼고, X 가 B 를 읽는데, 나온 내용은 Y 의 것이다.**
소유권·예약·요청 파라미터·완료 귀속이 모두 정확한 상태에서 이것이 성립하려면 남는 것은 둘뿐:

1. **X 의 쓰기가 애초에 Y 의 데이터를 실었다** (쓰기 경로 오염).
2. **읽기가 요청한 물리 위치가 아닌 곳의 데이터를 받았다** (요청은 맞고 반환이 틀림).

## 36.4 §32 의 쓰기 경로 감사를 재검증해야 한다

§32 는 WRAUDIT 로 "4 MiB 쓰기 1344/1344 정상"이라며 1 번을 배제했다.
그러나 **§34 이후 그 판정은 신뢰할 수 없다** — 그 런이 포맷 직후 첫 런이었는지,
즉 애초에 손상이 발생한 런이었는지 기록되어 있지 않다. 손상이 없던 런에서
"쓰기 버퍼가 깨끗하다"는 것은 아무것도 배제하지 못한다.

**다음: WRAUDIT 를 손상이 확인된 첫 런에서 다시 돌리되, `blk` 와 함께
`버퍼에 실제로 담긴 tid` 를 기록한다.** 그러면 FILLHASH 가 손상으로 지목한 바로 그 blk 에
대해 "쓸 때 이미 Y 였는가, 쓸 때는 X 였는데 읽을 때 Y 가 되었는가"가 한 번에 갈린다.
이것이 §36.3 의 두 갈래를 직접 가르는 유일한 측정이다.

---

# 37. 쓰기 경로 재검증 — 결백 확정, 그리고 갈래가 하나로 좁혀졌다 (2026-09-02)

§36.4 대로 WRAUDIT 를 **손상이 확인된 첫 런**(FAIL 44/160)에서 다시 돌렸다.
코드 수정은 필요 없었다 — WRAUDIT 는 이미 `blk_off` 와 `tid` 를 찍고 있었고,
빠져 있던 것은 **그 tid 를 쓰는 객체와 대조하는 조인**이었다(§32 의 실제 결함).
OWNER 의 `oid.lo = 0xC0FFEE00 + tid` 로 쓰는 객체의 tid 를 얻어 조인했다.

## 37.1 결과 — 쓰기는 결백하다 (이번엔 신호가 있는 상태에서)

| | |
|---|---|
| 쓰는 OID 와 버퍼 tid 를 모두 아는 블록 | 1,146,880 |
| **버퍼 tid ≠ 쓰는 객체** | **0** |
| 손상으로 지목된 읽기 영역의 블록 | 10,240 |
| **그중 버퍼 tid ≠ 쓰는 객체** | **0** |

§32 의 판정은 **재검증을 통과했다.** X 의 쓰기는 언제나 X 의 데이터를 실었다.
§36.3 의 갈래 1(쓰기가 남의 데이터를 실었다)은 **배제**된다.

### WRAUDIT MIXED 59 건은 오탐이다

이 런에서 `WRAUDIT MIXED` 가 59 건 나왔고 §32 의 0 건과 달라 보였으나, 전수 확인 결과
**전부 `blk_off` 2~2668, `tid0=0 round0=0`** 이었다. 데이터 청크는 `blk_off` 13,352,887 까지
뻗어 있다. 즉 이들은 블롭 앞쪽 **메타데이터 영역**이며, 0 으로 시작하는 버퍼가 WRAUDIT 의
"페이로드처럼 생겼는가"(`t0<64 && r0<64`) 필터를 통과한 것이다. 태그 페이로드가 아니다.
손상으로 지목된 블록과 겹치는 것도 **0/59** 다.

## 37.2 정정 — 한 블록은 매번 *같은* 남의 데이터를 준다

§36.3 에서 "같은 객체가 같은 블록을 두 번 읽어 매번 다른 tid 를 받는다"고 적었으나
이는 오독이었다. 같은 OID 의 **서로 다른 블록**이었다. 전수 확인 결과:

| | |
|---|---|
| 서로 다른 손상 blk | 5 |
| **두 가지 이상의 남의 페이로드를 돌려준 blk** | **0** |

`blk=12562359` 는 두 번 모두 tid=6 을 돌려줬다. **손상은 안정적이다** —
읽을 때마다 흔들리는 값이 아니라, 그 블록에 결부된 고정된 남의 데이터다.
이 정정은 방향을 바꾼다: 불안정했다면 전달 경로를 가리켰겠지만, 안정적이라는 것은
**주소 대응 자체가 어긋나 있음**을 가리킨다.

## 37.3 남은 갈래는 하나뿐이고, 두 형태다

확정된 사실을 나란히 놓으면:

- blk B 에 대한 **쓰기 버퍼는 X 의 데이터였다** (§37.1)
- blk B 를 쓴 것은 **X 뿐이다** (§33, §36.3)
- blk B 를 읽으면 **Y 의 데이터가 안정적으로 나온다** (§37.2)

세 가지가 동시에 참이려면 **논리 주소와 물리 위치의 대응이 어긋나야 한다.** 두 형태:

1. **빗나간 쓰기** — Y 의 쓰기가 Y 의 블록과 **B 에도** 내려앉았다. B 의 물리 내용이 실제로 Y 다.
2. **빗나간 읽기** — B 의 물리 내용은 X 인데, B 를 읽으려는 요청이 **Y 의 블록에서** 가져온다.
   주소 계산이 결정적으로 틀리면 반복 읽기에서도 같은 Y 가 나오므로 §37.2 와 모순되지 않는다.

## 37.4 다음 — 물리 내용을 DAOS 밖에서 확인한다

두 형태를 가르는 측정은 하나다: **손상된 blk B 의 물리 내용을 DAOS 를 거치지 않고 읽는다.**
`tests/spdk_blob_tagio.c` 의 blobstore 리더로 런 종료 후 B 를 직접 읽어,

- B 가 실제로 **Y** 를 담고 있으면 → **빗나간 쓰기** (형태 1)
- B 가 실제로 **X** 를 담고 있으면 → **빗나간 읽기** (형태 2)

더 싼 선행 측정도 있다: **정지 상태 재읽기.** 런이 끝나 동시성이 사라진 뒤 같은 객체를
다시 읽어, 여전히 Y 가 나오면 미디어/대응이 지속적으로 어긋난 것이고, X 가 나오면
손상이 동시성 창에 묶인 현상이라는 뜻이다. 이쪽을 먼저 한다.

---

# 38. ★★ 정지 상태 재읽기 — 손상은 미디어에 남는다. 빗나간 *쓰기*다 (2026-09-02)

§37.4 의 선행 측정. `obj_integrity.c` 에 `-A <round>` 감사 모드를 추가했다:
워크로드를 건너뛰고 **별도 프로세스에서 단일 스레드로 직렬 재읽기**만 수행해,
동시성이 완전히 사라진 뒤에도 마지막 라운드 태그가 남아 있는지 본다.

## 38.1 결과

| | |
|---|---|
| 손상 런 | **FAIL 33/160** |
| 정지 상태 감사 (별도 프로세스, 직렬, 동시성 0) | **AUDIT-FAIL 4/16 객체가 여전히 틀림** |

```
AUDIT t4  first bad at 4194304 (chunk-aligned): t5 r9 off 0        foreign-obj=524288
AUDIT t5  first bad at 0       (chunk-aligned): t4 r9 off 4194304  foreign-obj=1048576
AUDIT t8  first bad at 20971520(chunk-aligned): t4 r9 off 20971520 foreign-obj=524288
AUDIT t15 first bad at 20971520(chunk-aligned): t8 r9 off 20971520 foreign-obj=524288
```

**손상은 미디어에 지속된다.** 동시성이 없는 상태에서, 다른 프로세스로, 직렬로 읽어도
같은 남의 데이터가 나온다. 읽기 창에 묶인 일시적 현상이 아니다.

## 38.2 서명이 드러났다 — 청크가 서로 자리를 바꿨다

무작위 오염이 아니다. 두 가지 구조가 보인다.

**상호 교환 (t4 ↔ t5):**

| 객체 | 논리 오프셋 | 실제로 담긴 것 |
|---|---|---|
| t4 | 4 MiB | **t5** 의 **0** 오프셋 데이터 |
| t5 | 0 | **t4** 의 **4 MiB** 오프셋 데이터 |

**두 청크가 목적지를 맞바꿨다.**

**동일 오프셋 사슬 (t4 → t8 → t15):**

| 객체 | 논리 오프셋 | 실제로 담긴 것 |
|---|---|---|
| t8 | 20971520 | t4 의 **20971520** (같은 오프셋) |
| t15 | 20971520 | t8 의 **20971520** (같은 오프셋) |

**같은 논리 오프셋을 쓰는 객체들끼리** 목적지가 한 칸씩 밀렸다.
모두 **마지막 라운드(r9)** 의 데이터이고, 모두 **청크 정렬**이다.

## 38.3 §37.3 의 갈래가 결정됐다 — 형태 1

§37.3 은 두 형태를 남겼다. 정지 상태에서도 틀리므로:

- ~~형태 2: 빗나간 읽기~~ — **배제.** 읽기가 문제라면 미디어는 옳고, 부하 없는 재읽기는
  올바른 데이터를 돌려줬어야 한다.
- **형태 1: 빗나간 쓰기 — 확정.** 데이터가 물리적으로 잘못된 곳에 **영구히** 놓였다.

§37.1 과 합치면 결론은 하나다: **버퍼의 내용은 옳았고, 그것이 내려간 주소가 틀렸다.**
동시에 진행되는 두 쓰기가 목적지를 맞바꾼다. 이 조사가 처음부터 쫓던 "읽기 손상"은
사실 **쓰기 시점에 이미 벌어진 일**이었고, 읽기는 정직하게 그 자리에 있는 것을 돌려준 것뿐이다.

## 38.4 계측이 왜 이걸 못 봤는지도 설명된다

OWNER 와 WRAUDIT 는 **같은 `bio_iov` 의 주소를 읽는다**. 주소 배정 자체가 이미 어긋나 있다면
두 계측 모두 "일관되게 옳다"고 보고한다 — X 의 버퍼에 X 의 데이터가 있고, 그 biov 가 가리키는
blk 를 X 의 것으로 기록한다. 어긋남은 그 아래가 아니라 **주소가 정해지는 지점**에 있다.
§33 의 "쓴 OID == 읽는 OID" 도 같은 이유로 참이면서 무의미하다.

## 38.5 다음
1. **주소가 정해지는 지점을 좁힌다** — VOS 가 recx→extent 를 배정하는 경로(`vos_obj_update`
   → `vos_reserve_*` → biov 채우기)에서 **dkey/recx 와 배정된 blk 를 함께** 기록해,
   두 객체가 서로의 오프셋을 받는 순간을 잡는다. §31 의 VEA 계측은 예약의 *겹침*만 봤을 뿐
   **어느 요청에 어느 예약이 돌아갔는지**는 보지 않았다 — 그것이 정확히 이 구멍이다.
2. 재현 레시피 확정: 포맷 → 1 회 실행 → `-A` 감사. 손상이 **영구적이며 검증 가능**하므로
   업스트림 보고가 훨씬 강해진다(읽기 경합이 아니라 **데이터 유실/오배치**).

---

# 39. VOS 주소 배정 계측 — 여기도 깨끗하다. 창이 두 함수 사이로 좁혀졌다 (2026-09-02)

§38.5 대로 주소가 정해지는 지점을 계측했다. `srv_obj` 는 RPC 의 `orw_dkey` 를 갖고 있고
재현기는 dkey 를 청크 인덱스(uint64)로 쓰므로, OWNER 로그에 dkey 를 붙이면
**`(객체, 논리 청크) → 물리 블록` 대응이 쓰기·읽기 양쪽에서 그대로 드러난다.**
(`OWNER blk=.. op=R|W oid=.. dkey=.. firstw=..`)

## 39.1 결과 — 배정에 결함이 없다

손상 런(FAIL 15/160)의 로그에서:

| 검사 | 결과 |
|---|---|
| 쓰기 키 `(oid,dkey)` | 112 (= 16 객체 × 7 청크, 정확히 일치) |
| **두 개 이상의 `(oid,dkey)` 에 배정된 블록** | **0** |
| **그 키가 쓴 적 없는 블록으로 해석된 읽기** | **0** |
| 감사 패스에서도 중복 배정 | 0 |

**논리→물리 대응은 완전히 일관적이다.** 앨리어싱도 없고, 읽기가 엉뚱한 블록으로
해석되는 일도 없다. 형태 1 을 "VOS 가 주소를 잘못 준다"로 읽었던 §38.5 의 1 번은
**성립하지 않는다.**

## 39.2 그래서 창이 두 함수 사이로 좁혀졌다

확정된 사실을 다시 정렬하면:

| 지점 | 계측 | 확인된 것 |
|---|---|---|
| VOS 배정 | OWNER + dkey (§39.1) | `(X, 청크 c) → blk B`, B 는 X 만의 것 |
| `bio_iod_post` 직전 | WRAUDIT (§37.1) | biov 의 버퍼에 **X 의 데이터**가 들어 있다 |
| 미디어 | 정지 상태 감사 (§38) | blk B 에 **Y 의 데이터**가 영구히 있다 |

세 지점이 모두 참이므로, 어긋남은 **`bio_iod_post` 에서 SPDK 호출까지의 구간**에 있다.
즉 `dma_rw()` → DMA 매핑 → blob I/O 사이에서 **버퍼와 목적지의 짝이 뒤바뀐다.**
§38.2 의 상호 교환(t4↔t5) 서명은 정확히 이 형태다 — 두 개의 동시 쓰기가 짝을 맞바꾼다.

## 39.3 §30 도 재검증 대상이다

§30 은 `nvme_rw()` 를 계측해 "blob·채널·범위 정상, 예약 내 100 %, 발행:완료 1:1" 로
이 구간을 배제했다. 그러나 **§32 와 똑같은 결함이 있다** — 그 런이 포맷 직후 첫 런이었는지,
애초에 손상이 난 런이었는지 기록이 없다. 그리고 §30 은 `io_off` 가 *예약 범위 안*인지만
봤을 뿐, **그 `payload` 에 담긴 데이터가 그 `io_off` 의 주인 것인지**는 보지 않았다.
§32 가 "버퍼 내부 일관성만 보고 소유자와 대조하지 않은" 것과 같은 종류의 구멍이다.

## 39.4 다음 — 짝을 직접 확인한다

`nvme_rw()` 안에서 **`io_off` 와 그 순간 `payload` 에 실린 tid 를 함께** 기록한다.
그러면 §37.1(bio_iod_post 에서는 옳았다)과 조인해 한 번에 갈린다:

- nvme_rw 가 **(X 의 blk, Y 의 데이터)** 로 호출된다 → 짝이 **DAOS 안에서** 뒤바뀌었다
  (`dma_rw` 경로). DAOS 버그로 확정.
- nvme_rw 가 **(X 의 blk, X 의 데이터)** 로 호출된다 → 짝은 옳았고 SPDK/장치가
  잘못 내려앉혔다. §28·§29 의 단독 재현기가 깨끗했던 것과 정면으로 충돌하므로,
  그 재현기가 재현하지 못한 조건(동시 read/write 혼합 등)을 특정해야 한다.

---

# 40. nvme_rw 짝 계측 — SPDK 호출까지 전부 옳다. 손상은 *발행 이후*에 생긴다 (2026-09-02)

§39.4 대로 `nvme_rw()` 안, `spdk_blob_io_write()` 직전에 **그 순간 `payload` 에 실린 tid** 를
목적지 블록과 함께 기록했다(`PAIR blk= cnt= tid= round= off0= foreign=`).
`pg_idx` 는 OWNER 의 `blk` 와 같은 주소 공간이라 직접 조인된다.

## 40.1 짝은 옳다

손상 런 FAIL 70/160, 영구 손상 6/16 객체.

| 검사 | 결과 |
|---|---|
| PAIR 레코드 중 VOS 배정과 조인 가능 | **1120** (= 16 객체 × 7 청크 × 10 라운드, 정확히 일치) |
| **payload tid ≠ VOS 가 그 블록을 준 tid** | **0** |
| `io_off` 가 두 개 이상의 `pg_idx` 에서 도달됨 | 0 |
| `io_off / pg_idx` 비율 | 1.0 (단일값) |

**DAOS 가 SPDK 에 건네는 것은 주소도 데이터도 전부 옳다.**

## 40.2 지목 추적 — 올바른 블록에 올바른 데이터를 보냈는데 남의 것이 남는다

영구 손상된 t7 의 청크 0 을 끝까지 따라갔다:

| 단계 | 관측 |
|---|---|
| VOS 배정 (t7, 청크0, r9) | `blk 13245367` |
| SPDK 호출 시 payload | **tid=7 round=9 off0=0 foreign=0** (옳음) |
| 정지 상태 감사의 읽기 해석 | `blk 13245367` (같은 블록, 옳음) |
| 감사가 실제로 받은 내용 | **t4 의 r9 청크0** |

그리고 t4 의 r9 청크0 은 `blk 13244343` 으로 갔다 — **정확히 1024 블록(한 청크 4 MiB) 앞**,
배정 순서도 연속(seq 1704 → 1705)이다. 즉 **연속 배정된 인접 청크 사이에서** 내용이 밀렸다.

## 40.3 데이터 영역을 떠받치는 DMA 버퍼는 16 개뿐이다

`BLOBIO` 로그에서 데이터 영역(io_off ≥ 10⁶)의 I/O 에 쓰인 **서로 다른 payload 버퍼는 16 개**이며,
**16 개 전부가 여러 io_off 에 재사용**된다. 즉 DMA 청크 풀이 매우 공격적으로 재활용된다.
발행 시점의 내용은 옳으므로, **발행 이후에 그 버퍼가 다른 데이터로 덮이면** 장치가 뒤늦게
읽어 갈 때 남의 데이터를 옮기게 된다 — §38.2 의 상호 교환과 §40.2 의 인접 청크 밀림이
모두 이 형태다.

### 다만 "완료 전 재사용" 수치는 철회한다

이 로그로 "이전 I/O 가 완료되기 전에 버퍼가 재발행된 사건 70 건"을 셌으나, **`biod` 포인터가
재활용된다**(같은 주소가 나중에 다른 biod 로 다시 등장). `BLOBIO DONE` 은 `biod` 로만
식별되므로 완료 여부를 포인터 동일성으로 판정할 수 없다. **그 수치는 근거가 없어 철회한다.**
버퍼 재사용 자체(16/16)는 사실이지만, 그것이 *완료 전*이었는지는 이 계측으로 알 수 없다.

## 40.4 남은 두 가지와, 그것을 가르는 방법

- **발행 이후 버퍼가 덮인다** (조기 재활용) — DAOS 버그.
- **짝은 끝까지 옳았고 SPDK/장치가 잘못 내려앉힌다** — §28·§29 의 단독 재현기가 깨끗했던 것과 충돌.

포인터 동일성 추론을 버리고 **내용을 직접 재확인**하면 갈린다:
**`rw_completion()` 에서 각 영역의 payload 를 다시 스캔해, 발행 시점에 기록해 둔 tid 와 비교한다.**
발행 때 tid=7 이었는데 완료 때 tid=4 라면 **조기 재활용이 직접 증명된다** —
포인터가 재활용되든 말든 무관하다. 발행 시점 tid 를 영역별로 보관하려면
`struct bio_rsrvd_region` 에 디버그 필드 하나를 더하면 된다.

---

# 41. 완료 시점 재스캔 — 조기 재활용도 아니다. 결함은 SPDK API 아래에 있다 (2026-09-02)

§40.4 대로 포인터 동일성 추론을 버리고 **내용을 직접 재확인**했다.
`struct bio_rsrvd_region` 에 발행 시점 tid/round 를 새기고(`nvme_rw`),
`rw_completion()` 에서 그 영역의 payload 를 4 KiB 페이지마다 다시 훑어 비교한다
(`RESCAN OK` / `RESCAN CHANGED`).

## 41.1 결과

손상 런 FAIL 23/160, 영구 손상 1/16 객체.

| 영역 | RESCAN OK | RESCAN CHANGED |
|---|---|---|
| **데이터 (blk ≥ 10⁶)** | **1120** | **0** |
| 메타데이터/WAL (blk < 10⁶) | 1902 | 166 |

**데이터 영역에서 payload 는 발행과 완료 사이에 단 한 번도 바뀌지 않았다.**
(메타데이터의 166 건은 WAL 버퍼가 정상적으로 다시 채워지는 것이다.)

⇒ **§40.3 이 제기한 조기 버퍼 재활용은 배제된다.**

## 41.2 이제 DAOS 쪽 경로는 전부 소진됐다

| 지점 | 계측 | 결과 |
|---|---|---|
| VOS 주소 배정 | OWNER + dkey (§39) | `(X,청크)→blk B`, B 는 X 만의 것 |
| `bio_iod_post` 직전 버퍼 | WRAUDIT (§37) | X 의 데이터 |
| SPDK 호출 시 짝 | PAIR (§40) | 주소 X 의 것, 데이터 X 의 것 |
| **SPDK 완료 시 버퍼** | **RESCAN (§41)** | **여전히 X 의 데이터** |
| 미디어 (정지 상태) | 감사 (§38) | **Y 의 데이터, 영구히** |

DAOS 는 옳은 주소에 옳은 데이터를 건넸고, 장치가 그것을 읽어 갈 때까지 버퍼도 옳았다.
그런데 그 블록은 남의 데이터를 담고 있다. **결함은 `spdk_blob_io_write()` 의 API 계약 아래**,
즉 SPDK blobstore/bdev/드라이버 또는 장치에 있다.

## 41.3 §28·§29 와의 충돌을 해소해야 한다

이 결론은 §28(SPDK 드라이버 단독 재현기 0/2560)·§29(blobstore 단독 재현기 0/1280)와
정면으로 충돌한다. 따라서 **그 재현기들이 갖지 못한 조건**이 원인이어야 한다.
가장 두드러진 구조적 차이는 하나다:

> 단독 재현기는 **하나의 blob** 에만 I/O 했다.
> DAOS MD-on-SSD 는 **같은 blobstore 위의 여러 blob**(WAL, meta, data)에 동시에 I/O 한다.

§41.1 의 숫자가 이 방향을 강하게 시사한다 — 메타데이터/WAL 영역에서는 버퍼가 실제로
활발히 재사용되고 있고(166 건 변경), 그 트래픽이 데이터 blob 트래픽과 **같은 blobstore·같은
채널**을 공유한다. §40.2 의 "연속 배정된 인접 청크 사이 밀림"도 blob 내부 오프셋 변환
(`page2io_unit`) 이 blob 별 클러스터 맵을 거친다는 점과 맞물린다.

## 41.4 다음 (우선순위)
1. **다중 blob 단독 재현기.** `tests/spdk_blob_tagio.c` 를 확장해 **같은 blobstore 에 두 개 이상의
   blob** 을 만들고, 한쪽에 작은 쓰기를 지속적으로 흘리면서 다른 쪽에 4 MiB 태그 쓰기를 병행한다.
   §28·§29 가 놓친 조건을 정확히 재현하는 것이며, 성공하면 **DAOS 없이 SPDK 버그를 증명**한다.
   가장 값이 크다.
2. **물리 내용 직접 확인.** DAOS 정지 후 그 blobstore 를 단독으로 붙여 손상 blk 를 읽어,
   빗나간 쓰기(내용이 Y)인지 빗나간 읽기(내용이 X)인지 최종 확정한다.
3. **blob 클러스터 맵 덤프** — 두 blob 의 클러스터 맵이 겹치는지 확인(§31.2-3 의 미결 항목).
