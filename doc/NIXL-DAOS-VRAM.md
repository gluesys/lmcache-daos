<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# VRAM_SEG 실측 — GPU 직접 경로는 동작하지만 느리다 (2026-09-13, client-5)

## 요약

DAOS 객체를 GPU 메모리로 직접 읽고 쓰는 경로를 붙였다. **정합성은 통과한다.**
장치에서 만든 페이로드를 DAOS 에 쓰고 다른 GPU 버퍼로 되읽어 장치 쪽에서 비교했고
전부 일치했다.

**그런데 호스트 메모리 경유보다 3.4 배 느리다.** 원인은 우리 코드가 아니라
CaRT 가 GPU 버퍼를 전송마다 재등록하는 데 있고, 그 비용은 바이트가 아니라
**sgl 항목 하나마다 약 0.43 ms** 로 붙는다.

설계상 그것을 피하는 수단(`daos_mem_attr_t::ma_rkey`)은 존재하지만
**우리 구조에서는 쓸 수 없다.** §4 에 이유를 적는다.

결론: `VRAM_SEG` 는 **기본 비활성**으로 두고 experimental 로 표시한다.

## 1. 환경

client-5. DAOS 클라이언트는 `/opt/daos-gds-gpu`(미머지 `theodore/b_cufile`),
전송 스택은 `/opt/ofi-cuda`(CUDA 빌드 libfabric) + 패치된 mercury·UCX,
`D_MEM_DEVICE=1`, `D_GPU_DIRECT=1`. H100 NVL, ConnectX-7 400G RoCE.
풀 `attr1`, 컨테이너 `nixlgpu`.

client-5 에는 cuFile 스택이 완비돼 있다(`nvidia_fs` 적재, `/dev/nvidia-fs*`,
`libcufile.so`, `/etc/cufile.json`). 개발 호스트 cxl2 에는 없어서 이 작업은
client-5 에서만 가능하다.

## 2. 빌드 시점에만 켜진다

`daos_obj_fetch_gpu()` / `daos_obj_update_gpu()` 는 미머지 브랜치에만 있다.
stock 클라이언트에 대고 `VRAM_SEG` 를 광고하면 깔끔한 "미지원" 이 링크 오류나
더 나쁜 것으로 바뀐다. 그래서 meson 이 심볼 존재를 확인해 `NIXL_DAOS_HAVE_GPU`
를 정의할 때만 능력 목록에 들어간다.

```
DAOS prefix: /var/daos-stockfull
DAOS GPU-direct: not in this client (VRAM_SEG disabled)     ← 기본

DAOS_PREFIX=/opt/daos-gds-gpu
DAOS GPU-direct: available (VRAM_SEG enabled)               ← 명시적 선택
```

`DAOS_PREFIX` 를 환경변수로 둔 이유는 한 호스트에 두 클라이언트가 공존하기
때문이다. 어느 쪽을 쓸지는 탐색 순서로 추측할 일이 아니라 배치 결정이다.

## 3. 측정

객체 120 × 레이어 40 × 1 MiB = 4.69 GiB, inflight 64, 스레드 64.
스테이징 버퍼만 DRAM ↔ VRAM 으로 바꾼다.

| | 읽기 | 대역폭 |
|---|---|---|
| DRAM, 접기 | 154.3 ms | **32.61 GB/s** |
| DRAM, 접기 없음 | 349.6 ms | 14.39 GB/s |
| VRAM, 접기 | 1994.7 ms | **2.52 GB/s** |
| VRAM, 접기 없음 | 1884.2 ms | 2.67 GB/s |

**VRAM 은 접기가 듣지 않는다.** DRAM 은 접으면 2.3 배 빨라지는데 VRAM 은 오히려
미세하게 느려진다. 접기는 RPC 수를 4800 → 120 으로 줄이는 것이므로, 그것이 무효라는
말은 비용이 RPC 당이 아니라는 뜻이다.

### 비용은 sgl 항목당이다

총 바이트를 4.69 GiB 로 고정한 채 항목 수만 바꿨다.

| sgl 항목수 | VRAM | DRAM |
|---|---|---|
| 4800 (40 × 1 MiB) | 2080.9 ms / 2.42 GB/s | 150.5 ms / 33.45 GB/s |
| 1200 (10 × 4 MiB) | **516.0 ms / 9.75 GB/s** | 146.6 ms / 34.33 GB/s |
| 300 (10 × 16 MiB) | 637.9 ms / 7.89 GB/s | 625.2 ms / 8.05 GB/s |

4800 → 1200 은 항목 4 배 감소인데 시간이 **4.03 배** 줄었다. 이보다 깨끗한 비례는
없다. **항목당 약 0.43 ms.** 같은 구간에서 DRAM 은 150.5 → 146.6 ms 로 변화가 없다.

300 항목에서 둘 다 무너지는 것은 별개 현상이다. 레이어가 16 MiB 라 단일 akey 의
extent 가 커지고, `tests/obj_latency.c` 의 `one` 팔이 571 ms 로 가장 느렸던 것과
같은 패턴이다. **최적점은 중간에 있다.**

## 4. `ma_rkey` 는 우리가 쓸 수 있는 손잡이가 아니다

`daos_mem_attr_t::ma_rkey` 가 비어 있으면 CaRT 는 매번 `fi_mr_reg()` 로 GPU
버퍼를 등록한다. 소스가 그 상황을 그대로 적고 있다
(`daos-gds/src/client/cufile/cufile_plugin.c`).

> If ma_rkey is empty, CaRT falls back to normal fi_mr_reg() with FI_HMEM_CUDA
> — still correct, just redundant registration.

측정된 0.43 ms 가 그 "redundant registration" 이다.

채우면 `crt_bulk_import_rkey()` 로 가는데, **넣을 값이 우리에게 없다.**

```c
d_iov_set(&mem_attr.ma_rkey, (void *)rdma_info->desc_str, rdma_info->desc_len);
```

`rdma_info` 는 `cufileRDMAInfo_t` 이고, 이것은 **cuFile 드라이버가 등록된 파일
핸들로 IO 를 수행할 때 플러그인 콜백에 넘겨주는 것**이다. `cuFileBufRegister()` 를
우리가 부른다고 얻어지지 않는다. 즉 이 경로는 애플리케이션이 cuFile API 로 IO 를
하고 DAOS 가 그 아래 플러그인으로 동작할 때만 성립한다.

우리 NIXL 백엔드는 방향이 반대다. 우리가 DAOS 를 직접 부르므로 cuFile 이 개입할
자리가 없다.

전송별 제약도 하나 더 있다. `src/cart/crt_bulk.c` 가 UCX 에서는 raw rkey 임포트
자체를 거부한다(nvidia-fs 가 만든 등록을 UCX 경로로 가져올 수 없다). verbs 에서만
`crt_bulk_import_rkey()` 로 간다. 우리는 verbs 라 이 조건은 통과하지만, 제약이
하나 더 있다는 사실은 남는다.

## 5. MR 캐시도 막혀 있다

재등록을 캐시로 덮는 길도 닫혀 있다. `gpudirect/README.md` 에 기록된 대로
mercury `na_ofi` 가 `FI_MR_CACHE_MAX_COUNT=0` 을 강제하고 환경변수 덮어쓰기가
듣지 않는다. 이번 실행에서도 `FI_MR_*` 은 설정되지 않은 기본 상태였다.

## 6. 판정

- `VRAM_SEG` 는 **정합성 통과, 성능 미달**이다. 기본 비활성을 유지한다.
- 지금 KV 캐시 용도로는 **DRAM 경유가 3.4 배 빠르다.** GPU 직접이 유리하다는
  통념이 DAOS 의 이 구현에서는 성립하지 않는다.
- 다만 이 벤치는 스테이징 버퍼에서 끝난다. 실제로는 DRAM 팔이 거기서 GPU 로 한 번
  더 복사해야 하고 그 비용이 잡혀 있지 않다. **DRAM 에 유리한 측정**이므로 3.4 배는
  하한이 아니라 상한에 가깝다. 그래도 항목당 0.43 ms 를 덮을 규모는 아니다.

## 7. 상류 보고 후보

CaRT 가 GPU 버퍼 등록을 캐시하지 않는다는 것. `ma_rkey` 는 cuFile 플러그인
경로에만 열려 있고, 객체 API 를 직접 쓰는 클라이언트에는 재등록을 피할 수단이
없다. `theodore/b_cufile` 드래프트에 대한 피드백으로 쓸 수 있다.

## 재현

```bash
# client-5. GDS 전송 스택 필요
G=/opt/daos-gds-gpu
PRE=$(ls -d $G/prereq/release/*/lib64 | grep -v '/ofi/' | tr '\n' ':')
export LD_LIBRARY_PATH=/opt/ofi-cuda/lib64:$G/lib64:${PRE}/usr/local/cuda/lib64:...
export D_MEM_DEVICE=1 D_GPU_DIRECT=1
ulimit -l unlimited; ulimit -n 65536

./test_gpu   attr1 nixlgpu                       # 정합성
./bench_gpu  -p attr1 -c nixlgpu -o 120 -l 40 -s 1048576 -i 64 -r 2 -f 1 -g 1
```

플러그인은 `DAOS_PREFIX=/opt/daos-gds-gpu` 로 빌드해야 `VRAM_SEG` 가 켜진다.
