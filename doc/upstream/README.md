# 상류 제출 (최초 작성 2026-09-06, 갱신 2026-09-09)

이 디렉터리는 lmcache-daos GDS 작업에서 나온, 우리 리포 밖에서 고쳐야 하는 문제들을 다룬다.
근거 실측은 `gpudirect/README.md` 에 있다.

| 대상 | 파일 | 상태 |
|---|---|---|
| libfabric `prov/verbs` — dma-buf fd 누수 | `libfabric-0001-verbs-put-dmabuf-fd.patch`, `libfabric-pr-body.md` | **제출 준비 완료.** 브랜치 `hgichon/libfabric:verbs-put-dmabuf-fd` 푸시됨, PR 개설만 남음 |
| DAOS `theodore/b_cufile` 초안 — 복제 컨테이너 GPU 소스 쓰기 실패 | `daos-gds-replicated-write-report.md` | 재현 절차·서버측 증거·후보 기전 |
| LMCache — async prefetch 직렬화기 선택 옵션 | `lmcache-async-serializer-option.md` | 보류. 근거 실측이 experimental GDS 백엔드에서만 나와 우선순위를 낮춤 |

## libfabric fd 누수 — 제출 전 확인한 것

제출 직전 상류 `main`(2026-09-09 기준 `66aa5b7`)을 직접 읽고 네 가지를 확인했다. 초안 단계의 판단 중
두 개가 틀렸다.

1. **CUDA 라우팅 헝크는 제출 대상이 아니다.** 상류는 이미 `iface != FI_HMEM_SYSTEM` 으로 모든
   HMEM iface 를 dmabuf 경로로 보낸다. 우리 로컬 패치의 헝크 1 은 DAOS 가 번들하는 구버전 트리에만
   필요하다.
2. **`close(fd)` 는 상류에 맞지 않다.** 상류에는 `ofi_hmem_put_dmabuf_fd()` 라는 해제 API 가 이미
   있고(PR #10716, 2025-01-23 머지), iface 별로 분기한다. CUDA 는 `close(fd)` 지만 ROCR 은
   `hsa_amd_portable_close_dmabuf()` 를 호출해야 한다. 따라서 상류 제출본은 API 를 쓴다.
3. **verbs 는 fd 를 해제하지 않는 유일한 공급자다.** `ofi_hmem_get_dmabuf_fd()` 호출부는 cxi, opx,
   efa, verbs 넷인데 앞 셋은 모두 해제한다. 특히 efa 는 `ibv_reg_dmabuf_mr()` 바로 뒤에서 호출하며,
   이는 우리 패치와 형태가 같다. opx 는 헤더에 소유권 규약을 문서화해 두었다
   (`EXPORTED : obtained from ofi_hmem_get_dmabuf_fd(). Released by ofi_hmem_put_dmabuf_fd()`).
4. **중복 제출이 아니다.** 같은 유형의 누수가 fabtests 에서는 PR #11087 로 이미 고쳐졌지만
   verbs 공급자에 대한 이슈나 PR 은 없다.

측정치(1.5 s 만에 dmabuf fd 634 개 → 패치 후 1 개)는 `close(fd)` 로 얻은 것이다.
`cuda_put_dmabuf_fd()` 가 정확히 그 `close()` 이므로 제출본에 그대로 적용된다. PR 본문에 이 점을
밝혀 두었다. ROCR·ZE 하드웨어가 없어 그쪽은 시험하지 못했다는 것도 함께 적었다.

패치는 pristine `main` 클론에 `git apply --check` 로 깨끗이 적용됨을 확인했다. 컴파일은 이 개발
호스트에 verbs 헤더와 libtool 이 없어 하지 못했고 상류 CI 에 맡긴다. 새 include 는 필요 없다
(`ofi_util.h` → `ofi_mr.h` → `ofi_hmem.h` 로 선언이 이미 들어온다).

### 남은 절차

포크에 브랜치까지 올라가 있다. PR 은 다음 주소에서 열면 되고, 본문은 `libfabric-pr-body.md` 를
그대로 붙이면 된다.

```
https://github.com/ofiwg/libfabric/compare/main...hgichon:libfabric:verbs-put-dmabuf-fd
```

제목: `prov/verbs: release the dma-buf fd after registration`

## 환경 요약 (제출 시 첨부)

DAOS 2.9.100(stock 서버) + `theodore/b_cufile` 클라이언트, Mercury 2.4.1, libfabric **v1.22.0**
(CUDA dlopen 빌드), UCX 1.20, Rocky 10.2 / 커널 6.12, nvidia open 610.57, H100 NVL,
ConnectX-7 RoCE 400G(PFC 없음), DOCA 3.4(peermem 없음, dmabuf 만).

번들 의존성 버전은 DAOS `utils/build.config` 가 출처다(`ofi=v1.22.0`, `mercury=v2.4.1`,
`ucx=v1.20.0`). 이전 판에서 "libfabric 1.25" 로 적었던 것은 오기였다. libfabric 에 1.25 라는
릴리스는 없다.
