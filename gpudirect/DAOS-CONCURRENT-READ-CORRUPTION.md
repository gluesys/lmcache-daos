# DAOS 읽기 데이터 손상 — 최종 검토 결과, 상류 제출용 분석, 세션 인계

최종 검토 2026-09-01. 근거 커밋 `ee8439b` (브랜치 `streaming-get-and-client6-assets`).
재현기 `tests/dfs_integrity.c`, 보조 `tests/dfs_integrity_ab.sh`·`tests/agg_ab.sh`.
상세 서술 `README.md`, 배포 정보 `../deploy/MANIFEST.md`.

| | |
|---|---|
| **판정** | **DAOS 자체 결함.** 우리 패치·커넥터·LMCache·GPU-direct prereq 전부 측정으로 배제됨 |
| **영향 버전** | **2.8.0-rc3 와 2.9.100 동일** (둘 다 실측). 손상 신호를 만드는 코드는 두 버전 바이트 동일 |
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
| 2 | **2.8.0-rc3 도 동일하다.** 서버를 실제로 되돌려 측정: mixed 68/5120 = 1.33 %, 서명 동일. `dc_array.c`·`vos_aggregate.c`·`vos_csum_recalc.c`·`src/bio` 가 두 버전 **바이트 동일** | §13 |
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
| 2.9 신규 도입 / GPU-direct 백포트 | 2.8.0-rc3 에서 동률 재현 (§13.3) |
| 하드웨어 미디어 오류 | 전 장치 Media/Read/Write Errors 0 (§12.5) |
| DAOS-15847·DAOS-18901 (상류 fetch/aggregation 수정) | 2.9.100 엔 있고 2.8-rc3 엔 없는데 비율이 같은 자릿수 (§14.6) |

### 0.3 남은 미해결 (다음 세션의 일)

1. **근본 원인 미규명.** 남은 유력 방향은 **서버측 fetch/bulk 경로** — §12.8c 의 `hg_bulk_deserialize` 실패와 같은 뿌리일 가능성.
2. **DAOS-19569**(2026-08-31, affects 2.8·3.0, Awaiting backport): "multiple IODs" IOM 처리 오류, 본문에 *"possibly cause data corruption"*. 28 MiB 읽기는 정확히 multi-IOD 케이스다. component 가 EC 로 적혀 있어 RP 경로 해당 여부 확인 필요 (§14.4).
3. **`UCX_ENABLE_RCACHE=n`** 미시험. DAOS-18862 보고자가 양쪽에서 끈 상태였다 = UCX 등록 캐시를 의심했다는 뜻 (§14.3).
4. **§12.8b**(단일 스레드 덮어쓰기 세대만으로 재현)를 **건강한 풀에서** 결론내기. 열화된 풀에서 관측된 것이고, 2.8 에서도 포맷 직후엔 심했다가 치유됐다(§13.4).
5. **완전 상류 vanilla 서버**는 여전히 미검증. 단 2.8 서버는 GPU-direct 패치가 없는 별개 빌드(`port/2.8-wsd`, TAG 2.8.0-rc3, rkey 심볼 0)이고 거기서도 재현되므로 §12.8d 의 우려는 대부분 해소됐다.

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

# 13. 2.8.0-rc3 에서도 동일하다 (2026-09-01, 서버 reformat 실측)

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
