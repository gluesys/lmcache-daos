# DAOS 동시 읽기 데이터 손상 — 상류 제출용 분석 및 세션 인계

작성 2026-09-01. 근거 커밋 `e26cf27` (브랜치 `streaming-get-and-client6-assets`).
상세 조사 서술은 `README.md`, 배포 정보는 `../deploy/MANIFEST.md`.

이 파일은 두 목적을 겸한다 — **DAOS 상류 이슈 초안**과 **새 세션 인계서**.

> **2026-09-01 갱신: §12 를 먼저 읽을 것.** "DAOS 문제인가"는 그 날 **측정으로 확정**됐다
> (완전 스톡 클라이언트가 같은 비율로 실패 — §12.1). §12 는 §1·§2·§5·§10 을 대체한다:
> §1 의 "동시 읽기" 표현은 부정확하고(쓰기가 섞여야 한다 — §12.3), §5 의 서명은 모호한
> 패턴으로 얻은 것이라 §12.2 의 태그 페이로드 결과로 교체됐다.

---

## 1. 한 문장

**DAOS 2.9.100 에서, 여러 스레드가 각자 자기 파일을 동시에 읽으면 읽어온 데이터의 일부가
조용히 틀린다.** 28 MiB 객체 · 16 스레드에서 읽기당 **1~4%**. 반환값과 크기는 정상이므로
호출자가 알 수 없다.

## 2. 재현

**필요한 것: DAOS 클라이언트뿐. LMCache·GPU·torch·모델 전부 불필요.**

**권장 재현기는 이제 C 판이다** — `tests/dfs_integrity.c` (§12.2 의 태그 페이로드, Wilson
구간, arm 분리 플래그 포함). 아래 Python 판은 원본 기록으로 남긴다.

```bash
gcc -O2 -pthread -o dfs_integrity tests/dfs_integrity.c \
    -I$DAOS/include -L$DAOS/lib64 -ldfs -ldaos -ldaos_common -lgurt -lm \
    -Wl,-rpath,$DAOS/lib64
NA_UCX_EXTRA_TLS= ./dfs_integrity -p <pool> -c <cont> -s 28 -t 16 -r 40
```


```bash
DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> \
    python3 tests/test_rawio_integrity.py 28 16 13 hdr burst
```

- 인자: `chunk_MiB=28  threads=16  rounds=13  payload_offset=hdr(36B)  mode=burst`
- 컨테이너: POSIX, `--file-oclass=RP_2G4 --dir-oclass=RP_2G4 --oclass=RP_2G4
  --chunk-size=4194304 --properties=rd_fac:1`
- 각 스레드가 **자기 경로**(`/rawio_o36_t<tid>`)에 자기 고유 패턴을 쓰고 되읽어 비교한다.
  패턴은 위치 의존 + 스레드 id 의존이므로, 불일치가 "다른 스레드의 데이터" 인지 구분된다.
- `mode=burst` 는 쓰기 후 배리어를 걸고 **읽기를 동시에 발사**한다. `loop` 도 실패하지만
  burst 가 약간 더 자주 실패한다.
- 208 읽기(16×13)에서 보통 2~9건 실패. **0건이 나올 수 있으므로 한 번의 PASS 로 판단하면
  안 된다** — §6 참조.

`dfs_sys` 수준 호출만 쓴다 (`dfs_sys_open` / `dfs_sys_read` / `dfs_sys_close`,
목적지는 평범한 `bytearray` 를 `ctypes.from_buffer` 로 alias).

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
| DFS chunk 정렬 / 헤더 36 B straddling | 페이로드 오프셋 0·4 MiB 정렬에서도 재현 |
| 목적지 버퍼 타입 | `bytearray` 와 torch 백업 메모리 양쪽 재현 |
| 4 MiB chunk 크기 자체 | 32 MiB chunk 컨테이너에서도 재현(더 심한 형태) |
| **완전 스톡 prereq (mercury/UCX 포함)** | **2026-09-01 에 측정으로 배제됨 — §12.1.** 남은 마지막 변수였고, 이제 없다. |
| oclass·복제 | RP_2G1/G4/G8·SX(rd_fac 0)·S1 전부에서 재현 — §12.4 |
| 클라이언트 읽기 동시성 자체 | 조용히 쓴 객체를 16 스레드로 읽기만 하면 **0/4320** — §12.3 |

## 4. 스톡 대조 실험 (핵심 증거)

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

## 5. 손상 서명 (관측 원자료)

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

## 7. 환경 (다음 세션이 그대로 쓸 수 있는 상태)

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
  있으므로 DAOS 로 특정된 내용으로 갱신 필요.**

## 10. 다음 세션 작업 순서 (권고)

1. ~~**완전 스톡 prereq 로 재확인**~~ — **완료 2026-09-01, §12.1. 스톡도 같은 비율로 실패.**
   `/var/daosbuild/daos-stock` 에서 `--build-deps=yes` 로 mercury/UCX 까지 스톡 빌드
   (별도 prefix). draft 의 broken `0006_import_rkey.patch` 는 `841487de8` 에 존재하지
   않으므로 스톡 빌드가 성립한다. 여기서도 재현되면 상류 제출 근거가 완결된다.
2. ~~**재현기를 C 로 이식**~~ — **완료, `tests/dfs_integrity.c`.** `dfs_sys_open/read/close` 직접 호출하는
   단일 파일 C 프로그램이 상류에 훨씬 설득력 있다. `tests/dfs_gpu_rt.c` 구조를 재활용.
3. **최소 조건 탐색** — 일부 완료(§12.3·§12.4): 쓰기+읽기 혼합이 필요조건, oclass 는 무관. 스레드 수(4가 최소였는지 재확인),
   객체 크기, oclass(`SX`/`RP_2G1` 등), `rd_fac`, 서버 target 수를 한 번에 하나만 바꿔
   각 수백 검사로.
4. **서버 로그 확보** — 손상 발생 시각의 engine 로그(`D_LOG_MASK=DEBUG` 는 과하니
   `DD_SUBSYS=object,bio` 수준)와 클라이언트 `D_LOG_MASK=INFO` 를 함께.
5. **상류 제출** — daos-stack/daos 이슈. 포함: §1 한 문장, §2 재현 절차, §4 스톡 대조,
   §5 서명 원자료, §6 검정력 주의, 환경(2.9.100, `ucx+rc_v`, RP_2G4, chunk 4 MiB).
6. **Hub 정정 공지 갱신**(§9).

**그때까지 이 백엔드는 사용 불가로 유지한다.** 조용히 틀린 KV 를 서빙하기 때문이다.

## 11. 이 조사에서 철회한 주장 (반복 방지)

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

## 12.8 유력 가설 (미확정) — VOS aggregation

정황이 모두 한 곳을 가리킨다: **반복 덮어쓰기로 겹친 extent 를 VOS aggregation 이 병합할 때
chunk 하나가 다른 객체/에폭의 데이터로 바뀐다.**

근거: (a) 실패 창이 정확히 4 MiB 이고 손상 구간도 그 크기, (b) aggregation 자신의 checksum
재계산이 그 창에서 실패, (c) 쓰기 동시성이 있어야만 발생(겹친 extent 가 생김), (d) 클라이언트
빌드와 무관(서버 백그라운드 작업), (e) 이전 세션에서 libfabric·UCX 두 전송 모두에서 발생.

직접 A/B(`tests/agg_ab.sh`, `reclaim:lazy` vs `reclaim:disabled`, 런타임 pool 속성이라 서버
재시작 불필요):

| arm | 결과 |
|---|---|
| aggregation ON | 6/921 |
| aggregation OFF | **0/737** |

ON 비율(0.65 %)에서 737 읽기가 전부 통과할 확률은 ≈ 0.8 % 이므로 **시사적이지만 확정은
아니다**. 게다가 §12.7-1 의 EIO 때문에 두 arm 모두 표본이 잘렸다. **다음 세션 1순위는 이
A/B 를 제대로 검정력 있게 다시 하는 것**(한가한 풀, 짧은 런 다수, arm 교차, 각 arm 수천 읽기).

## 12.9 환경 (다음 세션이 이어받을 상태)

- cell1: `/var/daos-stockfull`(완전 스톡 2.9.100, 빌드 트리 `/var/daosbuild/build-stockfull`,
  로그 `/var/daosbuild/build-stockfull.log`, 스크립트 `/var/daosbuild/build_stockfull.sh`).
  기존 `/var/daosbuild/build-gpu` 와 별도라 패치 빌드는 그대로 살아 있다.
- client-5: `/var/daos-stockfull`(같은 경로 필수), `/root/dfs_integrity.c`,
  `/root/dfs_integrity_{patched,stock}`, `/root/dfs_integrity_ab.sh`. cell1: `/root/agg_ab.sh`.
- 새 컨테이너(gdspool): `ci_plain`(RP_2G4 4 MiB) `ci_csum`(+crc32) `ci_1m`(1 MiB chunk)
  `ci_s1` `ci_s2` `ci_s4` `ci_sx`. 풀 `reclaim` 은 **lazy 로 복원**해 두었다.
- 서버는 건드리지 않았다(패치 빌드 그대로 실행 중, 재시작 없음 — §8-1 준수).

## 12.10 다음 작업 순서 (개정)

1. **§12.8 aggregation A/B 를 검정력 있게 재실행.** 확정되면 상류 이슈의 제목이 바뀐다
   ("concurrent read corruption" → "VOS aggregation mis-merges a chunk").
2. `ci_1m` 외에 chunk 512 KiB·16 MiB 로 손상 구간 크기 = chunk 크기를 한 번 더 확인.
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
