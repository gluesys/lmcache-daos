# GPUDirect 스택 (`dfs_read_gpu` / `dfs_write_gpu`)

DAOS upstream 의 GPU-direct 초안을 **실제로 동작하는 상태**까지 올리는 데 필요한 패치와
검증 도구. 초안은 Draft 품질이라 그대로는 빌드조차 되지 않고, 빌드가 되어도 GPU 버퍼
등록이 실패한다. 여기 있는 6개 패치와 런타임 조건을 모두 갖췄을 때 GPU 왕복이 통과한다.

## 왜 cuFile 이 아닌가

계획 초안은 `CU_FILE_HANDLE_TYPE_USERSPACE_FS` 기반 cuFile 플러그인을 최종 경로로 삼지만,
실측 결과 이 경로는 현재 쓸 수 없다. 근거는 세 가지이고 서로 독립적이다.

- **cuFile 에 DAOS 드라이버가 없다.** `cufile.json` 의 fs 키는 `beegfs/gpfs/lustre/nfs/weka`
  뿐이고 `gdscheck -p` 드라이버 목록에도 DAOS 가 없다. dfuse 위의 `GdsBackend` 는 영구히
  compat(CPU 바운스)이다.
- **PCIP2PDMA 를 CUDA 가 거부한다.** cuFile 내부 로그가 `Failed to get cuda p2p device
  address ... errornum: 801` / `Not all GPUs support PCIP2PDMA` 를 남기고 전 파일시스템을
  `compat` 로 판정한다. `use_pci_p2pdma=true`, `use_legacy_p2p_allocation=1` 둘 다 판정을
  바꾸지 못했고, nvidia-fs 커널 카운터는 모든 시험에서 `Reads n=0` 이었다.
- **Userspace RDMA 가 미지원이다.** `gdscheck -p` 가 `Userspace RDMA : Unsupported`,
  `Mellanox PeerDirect : Disabled`, `rdma library : Not Loaded` 를 보고한다.

따라서 제품 경로는 **native `dfs_*_gpu()` + CUDA-aware UCX** 다. cuFile 은 표준 API 겉면으로
남겨두되 선택 사항이며, 이 저장소의 패치는 cuFile 플러그인을 빌드에서 제외한다.

## 소스 핀 (2026-08-28 조회)

| 항목 | 값 |
|---|---|
| DAOS 초안 브랜치 | `daos-stack/daos` `theodore/b_cufile` tip `c87080a70e39779f937a5a259c03b8c7ac56057c` |
| 초안 base | `841487de843eb97f72adb2904f4a2bb419685822` (master 계보, v2.8.0 의 조상 아님) |
| upstream master | `36399cdfadcc0bdf5c0100e603b98b4f5c2a16b9` |
| 번들 의존성 | Mercury `v2.4.1` (+DAOS 패치 6개), UCX `v1.20.0`, libfabric `v1.22.0` |
| 결과 버전 문자열 | `DAOS CLI 2.9.100, libdaos v2.8.0` |

초안 base 는 v2.8.0 과 분기해 있다(태그보다 218 커밋 앞, 태그에만 173 커밋). 릴리스
2.8.0 에 포팅하는 대신 **서버·클라이언트를 같은 초안 트리에서 빌드**하면 포팅 작업이
사라지고 wire 호환이 보장된다. 서버 측 diff 는 `srv_obj.c` +42줄(관측용)뿐이므로 서버에
CUDA 는 필요 없다.

## 패치 목록

| 패치 | 증상 | 원인 |
|---|---|---|
| `daos-0001-gate-cufile-sconscript` | `MissingDefinition('daos_common')` 로 빌드 중단 | 초안의 `src/client/cufile/SConscript` 이 feature-detect 없이 무조건 포함되고, prereq 가 아닌 내부 라이브러리를 `require()` 한다. `DAOS_BUILD_CUFILE=yes` 게이트를 넣어 기본 off |
| `daos-0002-enlarge-tse-task-arg-len` | `D_CASSERT(sizeof(obj_auxi_args) + sizeof(daos_task_args) <= TSE_TASK_ARG_LEN)` 실패 | 초안이 두 구조체에 `daos_mem_attr_t *` 를 추가해 예산을 넘겼다. `obj_internal.h` 주석이 지시하는 "enlarge tse_task" 를 따라 840 → 968 |
| `daos-0003-ucx-explicit-cuda-paths` | UCX configure 가 `CUDA support is requested but cuda packages cannot be found` | scons 가 경로 없는 `--with-cuda` / `--with-gdrcopy` 를 넘긴다. UCX 는 그 형태로 `/usr/local/cuda` 를 탐색하지 않는다 |
| `daos-0004-replace-broken-rkey-patch` | `error: corrupt patch at line 97` | 초안의 `deps/patches/mercury/0006_import_rkey.patch` 가 손상돼 있다. 헝크 라인 수가 틀리고, diff 뒤 `--` 종료자 다음에 산문(`Design notes:`)이 붙어 git·GNU patch 모두 파싱하지 못한다. 헝크 헤더의 문맥 문자열이 *그 패치가 추가한 함수명* 이라 git 생성물이 아니다. 63줄 정본 스텁으로 교체 |
| `ucx-0001-advertise-cuda-reg-via-dmabuf` | `ucp_mem_map()` = `Input/output error` | `uct_ib_check_gpudirect_driver()` 가 CUDA 등록 가능 여부를 **legacy peermem sysfs 3경로 존재로만** 판정한다. `uct_ib_md_check_dmabuf()` 는 dmabuf 지원을 따로 인식하지만 `reg_mem_types` 에 반영하지 않는다. DOCA 3.4 는 `ib_register_peer_memory_client` 를 export 하지 않아 이 게이트는 영구히 통과할 수 없다 |
| `mercury-0001-keep-cuda-memtype-tls` | `ucp_mem_map()` = `Invalid parameter` | `na_ucp_config_init()` 이 UCX TLS 를 provider 프로토콜명(`rc_v`)으로 고정한다. `cuda_copy`/`cuda_ipc` 는 네트워크 전송이 아니라 메모리 타입 컴포넌트인데 같은 TLS 목록으로 걸러지므로, dmabuf FD 제공자가 사라지고 `ibv_reg_mr()` 이 device 주소에 대해 EINVAL 을 낸다 |

`0004` 의 스텁 교체가 정당한 근거는 DAOS 코드 자체에 있다. `crt_bulk.c` 는 provider 가
UCX 면 `crt_bulk_import_rkey()` 를 **호출조차 하지 않고** HMEM 등록으로 넘어가며, OFI
경로도 실패 시 `-DER_NOSYS` → HMEM 폴백이다. rkey import 는 cuFile 이 등록한 키를
재사용할 때만 의미가 있고, 그 경로는 위에서 제외했다.

두 개의 상류 패치(`ucx-0001`, `mercury-0001`)는 **업스트림에 제출하지 않았다.** 둘 다
"dmabuf 만 있는 플랫폼" 에서의 회귀를 고치는 성격이라 upstream RFC 가치가 있다.

## 적용 순서

UCX·Mercury 소스는 첫 빌드가 의존성을 내려받은 뒤에야 존재하므로 2단계로 나뉜다.

```bash
# 0) 초안 브랜치 + 서브모듈 (초안은 raft 서브모듈 없이 클론하면 빌드가 깨진다)
git clone -b theodore/b_cufile --recurse-submodules \
    https://github.com/daos-stack/daos daos-gds

# 1) DAOS 트리 패치
./apply-patches.sh daos daos-gds

# 2) 서버 빌드 (CUDA 불필요)
cd daos-gds && scons --jobs "$(nproc)" --config=force --build-deps=yes \
    install PREFIX=/opt/daos-gds

# 3) GPU 클라이언트 빌드 (의존성 내려받기 + 전체 빌드)
scons --jobs "$(nproc)" --config=force --build-deps=yes install \
    BUILD_GPU_DIRECT=yes PREFIX=/opt/daos-gds-gpu BUILD_ROOT=$PWD/build-gpu

# 4) 상류 패치 → UCX·Mercury 만 수동 재빌드 (scons 를 다시 돌리지 않는다)
../apply-patches.sh deps daos-gds "$PWD/build-gpu"
```

**순서가 중요하다.** `scons --build-deps=yes` 는 prereq git 트리에 `git reset --hard` 를
하므로, deps 단계 패치를 적용한 뒤 scons 를 다시 돌리면 **UCX·Mercury 패치가 조용히
사라진다.** 빌드는 그대로 성공하고 런타임에 `ucp_mem_map()` 이 다시 `-EINVAL` 을 낼 뿐이라
알아채기 어렵다. DAOS 는 prereq 의 `.so` 를 경로로 적재하므로 두 컴포넌트를 PREFIX 로
직접 `make install` 하면 DAOS 재링크가 필요 없다 — `apply-patches.sh deps` 가 정확한
명령을 출력한다. 재빌드 후에는 scons 가 걸어두는 RPATH 를 다시 적용해야 모듈이 `libucs`
를 찾는다.

scons 는 `deps/patches/` 의 패치 파일을 `$BUILD_ROOT/external/release/<comp>__N` 으로
**복사해 두고 재사용**하므로, `0004` 처럼 패치 파일 자체를 고쳤을 때는 그 사본을 지워야
반영된다 (`apply-patches.sh daos` 가 처리한다).

### 빌드 호스트에 필요한 CUDA 패키지

`BUILD_GPU_DIRECT=yes` 는 CUDA 툴킷과 gdrcopy 를 요구한다. 컴파일에는 GPU 가 필요 없으므로
서버 노드에서 빌드해도 된다. 헤더 체인이 얕게 끊기므로 아래 네 개가 모두 필요하다.

```bash
cuda-cudart-devel-13-3   # cuda_runtime.h
cuda-driver-devel-13-3   # cuda.h, libcuda 스텁
cuda-nvcc-13-3           # crt/host_config.h  (cuda_runtime.h 가 include 한다)
cuda-nvml-devel-13-3     # nvml.h  (UCX configure 가 요구)
```

gdrcopy 는 배포 repo 에 없어 소스 빌드가 필요하다 (`make CUDA=/usr/local/cuda
prefix=/usr/local lib lib_install`). `/usr/local/cuda` 심볼릭 링크가 있어야 UCX 가 찾는다.

## 런타임 요구사항

- **nvidia open 커널 모듈.** closed 모듈(`kmod-nvidia-latest-dkms`)은 dma-buf export 를
  거부한다. perftest 가 `DMA-BUF is not supported on this GPU` 로 메모리 초기화 단계에서
  실패하는 것이 증상이다. `kmod-nvidia-open-dkms` 로 교체하면 같은 명령이 통과한다.
  `nvidia-fs`/GDS 도 open 모듈을 하드 요구한다.
- **`libcudart.so.13` 과 `libgdrapi.so.2` 가 클라이언트에 있어야 한다.** 없으면
  `libucm_cuda.so` 와 `libuct_cuda_gdrcopy.so` 가 dlopen 에 실패하고, UCM CUDA 훅이 없어
  memtype 추적이 깨진다. 증상은 `rcache: failed to insert region [0x0..0x0]: Invalid
  parameter` 다. UCX 를 빌드한 호스트에만 깔려 있으면 안 된다.
- **replicated object class.** 초안은 EC object 에 `-DER_NOTSUPPORTED` 를 반환한다
  (client-side EC/checksum 이 CPU 로 버퍼를 읽어야 하기 때문). 컨테이너를 `RP_2G1` 등으로
  만들어야 한다: `daos cont create <pool> <cont> --type POSIX --oclass=RP_2G1
  --dir-oclass=RP_2G1 --file-oclass=RP_2G1`.
- **에이전트 fabric domain 에 포트 접미사.** `daos_agent.yml` 의 `domain:` 은 `mlx5_0` 이
  아니라 **`mlx5_0:1`** 이어야 한다. 엔진은 접미사를 받지만 에이전트가 접미사 없는 값을
  넘기면 클라이언트가 `ucp_init() failed (No such device)` 로 죽는다.
- `peermem` 은 불필요하다 — 오히려 적재 자체가 불가능하다. DOCA 3.4 의 ib_core 는
  `ib_register_peer_memory_client` 를 export 하지 않는다 (`kallsyms` 조회 0).

## 검증 도구

세 단계로 좁혀 들어가도록 만들었다. 위에서 실패하면 아래는 볼 필요가 없다.

| 도구 | 범위 | 확인하는 것 |
|---|---|---|
| `../tests/dmabuf_mr.c` | DAOS·UCX·Mercury 배제, verbs 직접 | `cuMemGetHandleForAddressRange` dma-buf export → `ibv_reg_dmabuf_mr` 로 lkey/rkey 발급. 플랫폼 능력만 본다 |
| `../tests/ucp_cuda_reg.c` | UCX 만 | `ucp_mem_map(UCS_MEMORY_TYPE_CUDA)`. CaRT 가 UCX 로그 핸들러를 가로채 DAOS 안에서는 `UCX_LOG_LEVEL` 이 듣지 않으므로, 이 도구로 빼내 트레이스를 본다 |
| `../tests/dfs_gpu_rt.c` | 전체 경로 | `dfs_write_gpu()` → `dfs_read_gpu()` 왕복 + 정합성. 4 KiB / 64 KiB / 1 MiB / 32 MiB |

```bash
gcc -O2 -o dmabuf_mr dmabuf_mr.c -libverbs -lcuda
gcc -O2 -o ucp_cuda_reg ucp_cuda_reg.c -include string.h \
    -I$U/include -L$U/lib64 -lucp -lucs -luct -lcuda -Wl,-rpath,$U/lib64
gcc -O2 -o dfs_gpu_rt dfs_gpu_rt.c -I$P/include -L$P/lib64 \
    -ldaos -ldfs -lgurt -lcart -luuid -lcuda -pthread -Wl,-rpath,$P/lib64
```

세 프로그램 모두 CUDA 드라이버 API 를 직접 선언한다. CUDA 툴킷 헤더도 nvcc 도 필요 없이
클라이언트에서 바로 컴파일된다.

## 확인된 결과 (2026-08-29)

```
dfs_gpu_rt  proto v1        4096 / 65536 / 1048576 / 33554432 bytes  ALL OK
dfs_gpu_rt  proto v2 (기본)  4096 / 65536 / 1048576 / 33554432 bytes  ALL OK
dfs_gpu_rt  NA_UCX_EXTRA_TLS= (대조군)   ucp_mem_map() failed (Invalid parameter)
dmabuf_mr                    dma-buf export OK, ibv_reg_dmabuf_mr OK (lkey/rkey 발급)
ucp_cuda_reg  UCX_TLS=rc_v            Invalid parameter
ucp_cuda_reg  UCX_TLS=rc_v,cuda_copy  Success
ucx_perftest  호스트 메모리, rc_v, 4 MiB   14.1 GB/s   (기준선)
```

환경: client-5 (H100 NVL, DOCA 3.4, Rocky 10.2, 커널 6.12) ↔ cell1/cell2 (Rocky 8.10,
DAOS 2.9.100 초안, `provider: ucx+rc_v`, RoCE 400G NDR `mlx5_0`). el8 로 빌드한 DAOS
클라이언트 바이너리가 Rocky 10 에서 그대로 동작하므로 컨테이너 없이 붙였다.

`GPU0 ↔ NIC0` 은 `SYS`(소켓 횡단)다 — GPU 는 NUMA1, NIC 은 NUMA0. 아래 수치는 그
배치에서 나온 것이고 프로세스를 NUMA 에 고정하지 않았다.

### 먼저: 컨테이너 chunk size 를 지정하지 않으면 결론이 뒤집힌다

아래 수치를 읽기 전에 알아야 할 것. **DFS object chunk size 는 GPU-direct 의 이득을
결정한다.** `daos cont create` 에 `--chunk-size` 를 주지 않으면 기본 1 MiB 가 걸리고,
그 상태에서는 호스트 스테이징 경로가 포화하지 못해 GPU-direct 가 실제보다 훨씬 좋아
보인다. 같은 세션에서 두 컨테이너를 나란히 측정한 결과다 (8 GiB, 32 MiB 요청, 16 워커).

| 컨테이너 | `gpu` GB/s | `pinnedcopy` GB/s | `pinned` | `host` |
|---|---|---|---|---|
| `kvgds4m` (chunk 4 MiB) | 12.05 | **12.05** | 12.07 | 12.09 |
| `kvgds` (chunk 1 MiB, 기본) | 12.08 | **4.90** | 5.21 | 4.93 |

4 MiB 에서는 **모든 arm 이 같은 ~12.1 GB/s 로 수렴한다.** GPU-direct 는 대역폭 이득이
없다. 1 MiB 에서만 2.5× 차이가 나는데, 그것은 GPU 경로의 강점이 아니라 스테이징 경로가
1 MiB dkey 단위로 쪼개져 포화하지 못하는 것이다. 이 프로젝트는 이미 같은 현상을 관측한
적이 있다 — 메인 README 의 "chunk 1 MiB → 4 MiB 로 sustained read 9.2 → 34.5 GB/s".

모든 arm 이 같은 지점에서 멈추므로 그 ~12.1 GB/s 는 **클라이언트 메모리 경로 밖의 공유
한계**다(서버 엔진 xstream CPU 포화가 관측된 바 있어 서버가 유력하다). 즉 이 환경에서
GPU-direct 의 가치는 "천장을 올린다" 가 아니라 **"같은 천장에 도달하면서 호스트 DRAM 을
훨씬 덜 쓴다"** 다. 계획서 §8 이 세운 가정("pinned streaming 은 이미 raw DAOS 대역폭에
근접하므로 GDS 의 가치는 peak bandwidth 가 아니라 host DRAM/CPU 제거")이 그대로 확인된
셈이다. 단 CPU 쪽은 아래에서 보듯 DRAM 만큼 극적이지 않다.

`--chunk-size` 는 10진 접미사로 해석된다 (`4M` → 4,000,000). 32 MiB 요청과 정수로
나뉘도록 `--chunk-size=4194304` 처럼 정확한 값을 줄 것.

### 읽기 경로 비교 (chunk 1 MiB 컨테이너, 8 GiB / 32 MiB 청크, 단일 스레드)

주의: 아래 표는 **기본 1 MiB 컨테이너** 에서 측정한 것이므로 대역폭·지연·cycles 열의
arm 간 격차는 위에서 설명한 청크 효과를 포함한다. 청크에 영향받지 않는 것은 DRAM 열이다.

| arm | GB/s | 청크 지연 ms | cyc/byte | DRAM rd MiB | DRAM wr MiB | DRAM 배수 |
|---|---|---|---|---|---|---|
| `gpu` | **9.84** | **3.41** | **0.572** | 424 | 337 | **0.09** |
| `pinnedcopy` | 4.31 | 7.78 | 1.062 | 7765 | 8743 | 2.02 |
| `hostcopy` | 2.95 | 11.36 | 1.345 | 16262 | 18742 | 4.27 |
| `pinned` | 4.68 | 7.16 | 0.994 | 558 | 8704 | 1.13 |
| `host` | 4.50 | 7.46 | 1.020 | 638 | 8700 | 1.14 |

`hostcopy`(pinned 아님)는 CUDA 가 자체 pinned 바운스를 한 번 더 거쳐 DRAM 배수가 4.27
까지 오르므로 "스테이징" 대표값으로 인용하면 안 된다. 정직한 비교 대상은 `pinnedcopy` 다.

분해가 서로 맞물린다. `pinned`(H2D 없음)의 DRAM write 8.7 GiB 는 NIC 이 호스트 메모리로
DMA 한 8 GiB(쓰기 증폭 1.06×)이고, `pinnedcopy` 는 거기에 복사 엔진이 스테이징 버퍼를
읽는 7.8 GiB 를 더한다. `gpu` 의 0.09× 는 페이로드가 호스트 DRAM 을 아예 지나지 않고
제어 평면 트래픽만 남는다는 뜻이다 (유휴 기준선이 방향별 약 90 MiB/s 이므로 0.83초
구간의 약 75 MiB 는 배경 트래픽이다).

### chunk 4 MiB 컨테이너에서의 스펙 지표

같은 표를 튜닝된 컨테이너에서 다시 잰 것. 여기가 인용해야 할 수치다.

| 워커 | arm | GB/s | 청크 ms | cyc/byte | DRAM 배수 |
|---|---|---|---|---|---|
| 1 | `gpu` | 8.46 | 3.97 | 0.709 | **0.09** |
| 1 | `pinnedcopy` | 7.31 | 4.59 | 0.739 | 1.99 |
| 16 | `gpu` | 12.05 | 44.57 | 5.302 | **0.34** |
| 16 | `pinnedcopy` | 12.05 | 44.55 | 6.424 | 1.93 |

**남는 이득은 호스트 DRAM 트래픽 하나다.** 전달 바이트당 호스트 DRAM 이동량이 1 워커에서
0.09 vs 1.99 (**22×**), 16 워커에서 0.34 vs 1.93 (**5.7×**)이다. 청크 크기와 워커 수를
바꿔도 방향이 유지되는 유일한 지표다.

대역폭은 이득이 없다(12.05 vs 12.05). 지연도 포화점에서 같다(44.6 vs 44.5 ms). CPU
사이클은 1 워커에서 사실상 동일하고(0.709 vs 0.739) 16 워커에서 1.21× 인데, 이것도
내부적으로 일관된다 — pinned 메모리의 `cuMemcpyHtoD` 는 GPU 복사 엔진의 DMA 이므로 CPU
사이클이 아니라 **DRAM 대역폭**을 쓴다. 따라서 계획서가 "host DRAM/CPU 제거" 라고 묶어
표현한 것 중 실제로 제거되는 쪽은 DRAM 이고, CPU 는 거의 아니다.

> **정정.** 이 문서의 이전 판은 `pinnedcopy` 대비 "대역폭 2.28×, 포화점 2.44×, CPU
> 1.86× 개선" 이라고 적었다. 그 수치는 모두 기본 1 MiB 컨테이너에서 나온 것이고, 위에서
> 보듯 청크를 4 MiB 로 지정하면 사라진다. GPU-direct 의 근거로 대역폭이나 CPU 사이클을
> 들면 안 된다.

### Concurrency (chunk 1 MiB 컨테이너, 8 GiB / 32 MiB 청크)

| 워커 | `gpu` GB/s | 워커당 청크 ms | `pinnedcopy` GB/s | 워커당 청크 ms |
|---|---|---|---|---|
| 1 | 9.84 | 3.41 | 4.31 | 7.79 |
| 4 | 12.07 | 11.12 | 4.97 | 27.02 |
| 16 | 12.11 | 44.35 | 4.97 | 108.08 |
| 32 | 12.16 | 88.27 | 4.94 | 217.53 |

**붕괴하지 않는다.** 32 워커까지 처리량이 떨어지지 않고 4 워커에서 포화한 뒤 평평하다 —
계획서 §8 ④(registration churn 으로 처리량이 무너지지 않을 것)를 충족한다. 워커별 버퍼는
한 번 할당해 재사용하므로 이 sweep 이 보는 것은 등록 비용이 아니라 부하 상태의 전송
경로다. 포화 상태에서 워커당 지연이 워커 수에 선형으로 늘어나는 것은 총 처리량이 고정된
큐잉의 정상 거동이다. (`pinnedcopy` 열의 5 GB/s 평탄부는 청크 산물이다 — 4 MiB 에서는
12.05 까지 올라간다.)

`cyc/byte` 는 프로세스 전체 사이클을 전달 바이트로 나눈 값이라 **워커 수가 늘면 부풀려
진다** — DAOS 클라이언트가 이벤트 큐를 폴링하므로 대기 중인 스레드도 사이클을 태운다.
1 워커 값이 데이터 경로의 실제 비용에 가깝고, 16 워커 값은 arm 간 비교에만 쓸 것.

## 알려진 한계

- **측정 범위가 read 경로에 한정된다.** 대역폭·지연·cycles/byte·DRAM 트래픽·concurrency
  (1→32)는 측정했으나, **write 경로**(`dfs_write_gpu`)와 vLLM 수준의 TTFT 는 미측정이다.
  `GPU0↔NIC0=SYS` 배치에서 NIC/GPU 를 같은 root complex 로 옮기거나 프로세스를 NUMA 에
  고정하면 얼마나 달라지는지도 모른다.
- **~12.1 GB/s 공유 천장의 정체를 규명하지 않았다.** chunk 4 MiB 에서 `gpu`/`pinnedcopy`/
  `pinned`/`host` 네 arm 이 모두 그 값에서 멈추므로 클라이언트 메모리 경로 밖의 한계이고,
  서버 엔진(호스트당 1 엔진, `targets 8 / helpers 2`, 과거 xstream CPU 94% 포화 관측)이
  가장 유력하다. 엔진을 늘려 확인하면 GPU-direct 의 실제 상한도 함께 드러난다.
- **rkey import 는 스텁이다.** `HG_Bulk_import_rkey()` 가 `HG_OPNOTSUPPORTED` 를 돌려주고
  호출자가 HMEM 등록으로 폴백한다. UCX 경로에서는 애초에 호출되지 않으므로 손실이 없지만,
  OFI/verbs + cuFile 조합을 쓰려면 진짜 구현이 필요하다.
- **proto v2 SEGV.** CUDA 메모리 타입이 탐지되지 않던 상태에서는 UCX proto-v2 의
  `ucp_wireup_replay_pending_request()` 에서 SEGV 가 났다. 위 패치들을 적용한 뒤에는
  재현되지 않는다 — 원인을 따로 규명한 것은 아니므로, 다시 나타나면
  `UCX_PROTO_ENABLE=n` 으로 격리한 뒤 이 항목을 다시 볼 것.
- **LMCache 연동이 없다.** 이 디렉터리는 DAOS 쪽 데이터 평면만 다룬다. `DaosGdsBackend`
  와 v2 저장 포맷은 별도 작업이다.
