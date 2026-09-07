# 상류 제출 초안 (2026-09-06)

이 디렉터리는 lmcache-daos GDS 작업에서 나온, 우리 리포 밖에서 고쳐야 하는 문제들의 제출 초안이다. 실제 제출(PR/이슈 개설)은
계정·채널 결정이 필요해 여기서는 **바로 붙일 수 있는 텍스트와 패치**까지만 준비한다. 근거 실측은 `gpudirect/README.md` 에 있다.

| 대상 | 파일 | 상태 |
|---|---|---|
| libfabric `prov/verbs` — dma-buf fd 누수 | `libfabric-0001-verbs-close-dmabuf-fd.patch` | upstream `main` 기준 초안. **CUDA 를 dmabuf 경로로 보내는 우리 패치의 1 헝크는 upstream main 에 이미 반영됨**(`iface != FI_HMEM_SYSTEM` 조건) — DAOS prereq 의 1.25 에만 필요. fd close 는 main 에도 없음 → 제출 대상 |
| LMCache — async prefetch 직렬화기 선택 옵션 | `lmcache-async-serializer-option.md` | `dev` 브랜치도 `AsyncSingleSerializer` 하드코딩 확인(2026-09-06). 제안 diff + 근거 |
| DAOS `theodore/b_cufile` 초안 — 복제 컨테이너 GPU 소스 쓰기 실패 | `daos-gds-replicated-write-report.md` | 재현 절차·서버측 증거·후보 기전 |

제출 시 첨부할 환경 요약: DAOS 2.9.100(stock 서버) + `theodore/b_cufile` 클라이언트, Mercury 2.4.1, libfabric 1.25(CUDA dlopen 빌드), UCX 1.20,
Rocky 10.2 / 커널 6.12, nvidia open 610.57, H100 NVL, ConnectX-7 RoCE 400G(PFC 없음), DOCA 3.4(peermem 없음, dmabuf 만).
