# 배포 매니페스트 — 어느 호스트가 어느 DAOS 빌드를 쓰는가

이 파일이 존재하는 이유는 하나다. **서버를 GDS 용으로 재구축했는데 컨테이너가 예전
클라이언트를 계속 쓰고 있었고, 아무도 그것을 볼 수 없었다.** 그 상태에서 동시 다중 chunk
읽기가 4 MiB 한 덩어리씩 조용히 깨졌다 — 크기도 반환코드도 정상이었다. 상세는
`../gpudirect/README.md`.

빌드를 **파일 이름으로 구분할 수 없다**는 점이 문제를 키웠다. DAOS 는 버전과 무관하게
`libdaos.so.2.8.0` 이라는 이름을 쓴다. 그래서 이 매니페스트는 **크기와 심볼**로 신원을
기록한다.

- `libdaos` 크기 — 빌드가 다르면 다르다
- `HG_Bulk_import_rkey` 심볼 유무 — `gpudirect/patches/daos-0004` 가 넣는 스텁이므로
  **GDS 패치 적용 여부의 지표**다 (있음=패치됨)

`deploy/check_manifest.sh` 가 실제 호스트에서 같은 값을 뽑아 아래와 비교한다. 문서만
두면 드리프트하므로 반드시 그 스크립트로 확인할 것.

## 기록 시점: 2026-08-31

DAOS 소스: `c87080a70` (`theodore/b_cufile`, "feat: Add DAOS cuFile userspace FS
plugin prototype"), 빌드 트리 `cell1:/var/daosbuild/daos-gds`

| 호스트 | 경로 | libdaos 크기 | rkey 스텁 | 빌드일 | 역할 |
|---|---|---|---|---|---|
| cell1 | `/opt/daos-gds` | 8950504 | 있음 | 08-28 | **engine + agent 실행중** |
| cell1 | `/opt/daos-gds-gpu` | 8950616 | 있음 | 08-28 | 미사용 |
| cell1 | `/opt/daos` | 9135472 | 없음 | 08-23 | pre-GDS, 미사용 |
| cell2 | `/opt/daos-gds` | 8950504 | 있음 | 08-28 | **engine + agent 실행중** |
| client-5 | `/opt/daos-gds-gpu` | 8950616 | 있음 | 08-28 | **agent 실행중** |
| client-6 | `/opt/daos-gds-gpu` | 8950616 | 있음 | 08-28 | **agent 실행중** |
| client-6 | `/root/daoslibs29` | 8950616 | 있음 | 08-28 | **컨테이너가 마운트하는 번들 (현재)** |
| client-6 | `/root/daoslibs` | **2127664** | **없음** | **08-23** | ⚠️ 구 번들. 원인 B 의 주체. 참고용으로만 보존 |

## 알려진 비대칭 두 개

**1. 서버와 클라이언트가 다른 빌드다.** 서버는 `/opt/daos-gds`(8950504), 클라이언트는
`/opt/daos-gds-gpu`(8950616). 같은 소스에서 나온 다른 빌드다(GPU/cuFile 옵션 차이로 보임).
이 조합은 **검증됐다** — `tests/test_rawio_integrity.py` 28 MiB × 16 스레드 × 5 라운드
80/80 바이트 일치. 다만 "같은 소스니까 괜찮다" 가 아니라 **이 쌍이 통과했다**는 것만
확인된 상태다.

**2. `/root/daoslibs` 를 지우지 않았다.** 원인 B 를 재현하거나 대조군으로 쓸 수 있어
남겼다. **컨테이너가 이것을 마운트하면 안 된다.** 런처는 `daoslibs29` 를 가리킨다.

## 규칙

1. **서버를 재구축하면 클라이언트 번들도 다시 만든다.**
   `deploy/mk_daoslibs_bundle.sh <daos-prefix> <out-dir> <template-dir>`
2. **번들을 만든 뒤 증명한다.** 실패가 조용하므로 믿지 말고 측정한다.
   ```
   DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> \
       python3 tests/test_rawio_integrity.py 28 16 5 hdr     # 80/80 요구
   ./tests/kv_correctness_gate.sh                             # 생성 동일성
   ```
3. **`NA_UCX_EXTRA_TLS` 는 비워둔다.** GDS 패치가 적용된 클라이언트에서 이 값에
   `cuda_copy,cuda_ipc` 가 들어가면 호스트 메모리 전송이 깨진다
   (`gpudirect/patches/mercury-0001` 헤더 참조). 런처가 명시적으로 비워 설정한다.
4. **이 표를 갱신하고 `check_manifest.sh` 로 확인한다.**

## 이 매니페스트가 커버하지 못하는 것

- 컨테이너 이미지(`kvsup:052`)의 내용 — LMCache 휠 버전, torch CUDA 빌드. 별개 문제가
  있다: 이미지의 LMCache 0.5.2 휠은 CUDA 13 으로 빌드됐고 torch 는 cu128 이라
  **fused `c_ops` 가 로드되지 않고 Python 폴백으로 동작한다**(`gpudirect/README.md`).
- 서버 풀/컨테이너 속성. `deploy/config/lmcache-daos.yaml` 주석에 생성 명령이 있다.
