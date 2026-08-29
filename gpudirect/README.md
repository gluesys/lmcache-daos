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

### 성능은 컨테이너 구성에 지배된다 — 먼저 이것을 맞춰야 한다

`daos cont create` 의 두 기본값이 결과를 좌우한다. 둘 다 지정하지 않으면 호스트 스테이징
경로만 심하게 불리해져 GPU-direct 가 실제보다 훨씬 좋아 보인다.

- **`--chunk-size`**: 미지정 시 1 MiB. 10진 접미사로 해석되므로(`4M` → 4,000,000) 32 MiB
  요청과 정수로 나뉘도록 `--chunk-size=4194304` 를 줄 것.
- **`--file-oclass`**: `RP_2G1` 의 `G1` 은 **placement group 1개** 라는 뜻이다
  (`OC_RP_2G1 = OBJ_CLASS_DEF(OR_RP_2, 1ULL)`). DFS 파일 하나 = array object 하나이므로
  파일 전체가 2 타깃에만 놓인다. `RP_2GX` 를 주면 타깃 수에 맞춰 해석된다(이 클러스터에서는
  `RP_2G4`).

같은 세션에서 측정한 구성 민감도 (8 GiB, 32 MiB 요청, `gpu` arm):

| 구성 | 1W | 4W | 16W | 32W |
|---|---|---|---|---|
| chunk 4 MiB + `RP_2G1` | 8.60 | 12.15 | 12.09 | 12.12 |
| chunk 4 MiB + `RP_2G4` | 7.16 | 12.11 | **20.49** | **21.50** |

`RP_2G1` 의 12 GB/s 평탄부는 서버 CPU 한계가 아니었다. 부하 중 엔진 xstream 10개가 각
99.7% 로 포화한 것은 사실이지만(96코어 중 ~9.1코어), 그것은 원인이 아니라 결과였다 —
2 타깃으로 좁혀진 트래픽이 그 타깃들의 xstream 을 태우고 있었을 뿐이다.

### 최종 수치 (대칭 8+8 NVMe, chunk 4 MiB, `RP_2G4`)

| 워커 | arm | GB/s | 청크 ms | cyc/byte | DRAM 배수 |
|---|---|---|---|---|---|
| 1 | `gpu` | 5.72 | 5.87 | 0.764 | **0.10** |
| 1 | `pinnedcopy` | 11.09 | 3.03 | 0.723 | 1.93 |
| 1 | `pinned` | **14.29** | 2.35 | 0.453 | 1.11 |
| 16 | `gpu` | 24.39 | 22.01 | 3.848 | **0.23** |
| 16 | `pinnedcopy` | **35.62** | 15.07 | 2.150 | 1.79 |
| 16 | `pinned` | 34.87 | 15.40 | 5.380 | 1.13 |
| 16 | `host` | 16.79 | 31.97 | 0.742 | 1.20 |
| 16 | `hostcopy` | 6.47 | 83.02 | 1.094 | 4.58 |

`pinned` 이 16 워커에서 34.9 GB/s 로 메인 README 의 과거 기록(34.5 GB/s sustained read)을
재현한다 — 구성이 제대로 잡혔다는 확인이다.

**GPU-direct 는 대역폭과 지연 모두 진다** — 16 워커에서 스테이징의 0.68배(24.4 vs 35.6),
단일 워커에서 0.52배(5.7 vs 11.1). 세 가지 서로 다른 타깃 구성(7+8 비대칭, 8+8 대칭)에서
`gpu` 는 19~24 GB/s 대역에 머무는데 호스트 경로는 35 GB/s 까지 올라가므로, GPU 경로에 자체
상한이 있는 것으로 보인다.

**유일하게 남는 이득은 호스트 DRAM 트래픽이다.** 전달 바이트당 0.10 vs 1.93 (1워커,
**19×**), 0.23 vs 1.79 (16워커, **7.8×**). 청크 크기·oclass·타깃 수·워커 수를 모두 바꿔도
방향이 유지되는 지표는 이것뿐이다. 분해도 맞물린다 — `pinned`(H2D 없음)의 DRAM write 는
NIC 이 호스트로 DMA 한 8 GiB 이고, `pinnedcopy` 는 복사 엔진이 그것을 되읽는 몫을 더한다.
`gpu` 는 둘 다 없다.

`cyc/byte` 는 워커 수가 늘면 부풀려진다 — DAOS 클라이언트가 이벤트 큐를 폴링하므로 대기
중인 스레드도 사이클을 태우고, 느린 arm 이 바이트당 더 오래 돈다. 1 워커 값이 데이터 경로
비용에 가깝고, 그 값에서는 `gpu` 와 `pinnedcopy` 가 사실상 같다(0.764 vs 0.723). pinned
메모리의 `cuMemcpyHtoD` 는 GPU 복사 엔진의 DMA 이므로 CPU 사이클이 아니라 DRAM 대역폭을
쓴다는 것과 일관된다.

따라서 이 경로의 정당화는 "빠르다" 가 아니라 **"같은 데이터를 호스트 DRAM 을 거의 쓰지
않고 가져온다"** 로만 가능하다. 그 이득이 값어치가 있는지는 실제 vLLM 워크로드에서 호스트
DRAM 대역폭이 실제로 경합하는지에 달려 있고, 그것은 아직 측정하지 않았다.

### 토폴로지는 원인이 아니다 (검증 완료)

GPU 와 NIC 이 같은 NUMA 노드에 있는 호스트(client-6, `nvidia-smi topo` = `NODE`)와 서로
다른 노드인 호스트(client-5, `SYS`)에서 **같은 풀·컨테이너·파일**을 읽어 비교했다. 두
장비는 동일 모델·동일 커널(6.12.0-211.16.1)이고 GPU/NIC 의 PCI 주소도 같다 — 차이는 BIOS
의 SNC 설정뿐이다(client-5/7 은 SNC 로 소켓0이 node0/node1 로 쪼개져 GPU 가 node1, NIC 이
node0 이 된다. client-6 은 SNC 가 꺼져 둘 다 node0).

5회 반복, 중앙값과 범위:

| 배치 | arm | 워커 | 중앙값 GB/s | 범위 |
|---|---|---|---|---|
| `NODE` (client-6) | `gpu` | 1 | 8.27 | 6.20–8.76 |
| `SYS` (client-5) | `gpu` | 1 | 7.89 | 7.31–9.24 |
| `NODE` | `gpu` | 16 | 21.61 | 18.74–25.37 |
| `SYS` | `gpu` | 16 | 19.16 | 15.07–24.20 |
| `NODE` | `pinnedcopy` | 1 | 11.25 | 11.18–11.28 |
| `SYS` | `pinnedcopy` | 1 | 11.26 | 11.13–11.33 |
| `NODE` | `pinnedcopy` | 16 | 35.81 | 34.23–36.01 |
| `SYS` | `pinnedcopy` | 16 | 33.93 | 27.73–34.12 |

**토폴로지로 설명되지 않는다.** GPU-direct 는 NODE 에서 중앙값이 +5%(1워커)·+13%(16워커)
높지만 범위가 크게 겹쳐 노이즈와 구분할 수 없다. 무엇보다 이상적 배치에서도 스테이징
대비 비율이 그대로다 — 0.74×(1워커, 8.27 vs 11.25), 0.60×(16워커, 21.61 vs 35.81). 즉
GPU 경로의 열세는 GPU/NIC 를 같은 노드에 두어도 사라지지 않는다.

`nvidia-smi topo -m` 의 `SYS` 표기는 애초에 오해를 부른다. client-5/7 에서 node0↔node1 은
**같은 소켓 안의 SNC 분할**이고 거리 12(소켓 횡단은 21)이므로 UPI 를 건너지 않는다.
"소켓을 건너기 때문" 이라는 설명은 처음부터 성립하지 않았다.

**부수 발견: GPU 경로만 불안정하다.** 16워커에서 `pinnedcopy` 는 34.2–36.0(±2.5%)인데
`gpu` 는 18.7–25.4(±15%)로 흔들린다. 1워커에서도 `pinnedcopy` 가 11.18–11.28 인 반면
`gpu` 는 6.20–8.76 이다. 원인은 규명하지 않았고, 남은 후보는 GPU BAR 로의 PCIe 쓰기
대역폭과 UCX 가 device memory 를 다루는 방식(전송별 등록/rendezvous)이다.

### 전송 계층에서 본 원인: GPU BAR 쓰기는 QP 당 제한된다

DAOS 를 배제하고 두 GPU 호스트(client-5 ↔ client-6, 동일 perftest 6.29, 동일 400G NDR)
사이에서 직접 쟀다. DAOS 객체 fetch 는 **서버가 클라이언트 버퍼로 push(RDMA write)** 하므로
write 방향이 관심 대상이고, read 는 대조군으로 함께 쟀다.

| 연산 | 크기 / QP | 대상=호스트 | 대상=GPU |
|---|---|---|---|
| write | 4 MiB, 1 QP | 46.24 | **22.07** |
| write | 4 MiB, 4 QP | 46.28 | **39.89** |
| write | 32 MiB, 1 QP | 46.29 | 30.85 |
| read | 4 MiB, 1 QP | 10.54 | **10.59** |
| read | 4 MiB, 4 QP | — | 37.56 |
| read | 4 MiB, 8 QP | — | 40.54 |

세 가지가 나온다.

- **GPU 페널티는 write 에만 있다.** 1 QP write 는 호스트 46 vs GPU 22 GB/s 인데, read 는
  호스트 10.54 vs GPU 10.59 로 **동일**하다. GPU 메모리가 느린 것이 아니라 GPU BAR 로의
  RDMA write 가 QP 당 제한된다.
- **하드웨어 한계가 아니다.** QP 를 늘리면 write 39.89, read 40.54 GB/s 까지 나온다.
- **전송 단위가 클수록 유리하다.** 1 QP write 가 4 MiB 에서 22.07, 32 MiB 에서 30.85 다.

그리고 **DAOS GPU-direct 의 천장 19~24 GB/s 는 1 QP GPU write 천장(22)과 일치한다.** 이것이
열세의 직접적인 설명이다 — 토폴로지가 아니라 GPU BAR 쓰기의 QP 당 대역폭이다.

#### 그룹 폭을 넓혀도 해결되지 않는다 (오히려 나빠진다)

서버 측 병렬 엔드포인트를 늘리면 QP 수 효과를 볼 수 있을 것으로 보고, 그룹 폭만 다른
컨테이너 세 개를 만들어 쟀다 (16 워커, 8 GiB, chunk 4 MiB 동일, client-6).

| oclass | `gpu` 중앙값 (범위) | `pinnedcopy` 중앙값 |
|---|---|---|
| `RP_2G1` | 12.06 (11.88–12.11) | 11.67 |
| `RP_2G4` | **20.01** (16.48–22.44) | 34.02 |
| `RP_2G8` | **12.65** (12.20–16.76) | 34.77 |

스테이징은 G4 → G8 에서 34.0 → 34.8 로 영향이 없는데 GPU 는 20.0 → 12.7 로 **떨어진다.**
위의 "전송 단위가 클수록 유리하다" 와 맞물리는 결과다 — 그룹을 넓히면 32 MiB 요청이 더
많은 샤드로 쪼개져 전송 단위가 작아지고, 그 손해를 GPU 경로만 부담한다. 이 환경에서
GPU-direct 의 최적점은 `RP_2G4` 이고, 더 넓히면 손해다.

#### UCX 노브로는 재현되지 않았다

`UCX_MAX_RNDV_LANES` 는 **존재하지 않는 변수**다(`ucx_info -f` 에 없음) — 그것으로 한
초기 실험은 전부 무효였다. 실제로 있는 것은 `UCX_MAX_RNDV_RAILS`(기본 2),
`UCX_RNDV_SCHEME`, `UCX_RNDV_FRAG_SIZE=host:512K,cuda:4M`, `UCX_MIN_RNDV_CHUNK_SIZE` 다.
다만 rails 는 **여러 디바이스를 병렬로 쓰는 멀티레일** 설정이라 NIC 포트가 하나인 이
환경에서는 perftest 의 `-q 4`(같은 포트에 QP 4개)를 재현할 수 없다. 서버 엔진에 주입해
확인하려 했으나 daos_server 재시작이 아래의 SPDK wedge 를 유발해 측정에 이르지 못했다.

### DRAM 이득의 값어치: 재봤더니 결정적이지 않다

남은 유일한 근거가 호스트 DRAM 절약이었으므로, 그 절약이 실제로 부족한 자원을 푸는지
측정했다. 필요한 것은 분자(각 경로의 DRAM 소비)와 분모(그 호스트의 DRAM 여력)다.

**분모 — client-6 의 DRAM 대역폭** (2× Xeon 8558, DDR5-4800 × 16 DIMM, 1007 GB):

| 모드 | 64 스레드 |
|---|---|
| LOAD (읽기만) | 214.8 GB/s |
| COPY (읽기+쓰기) | 136.6 GB/s |

`../tests/bench_dax_bw.c` 의 `anon` 모드로 쟀다. 단순 언롤 루프이므로 **하한 추정**이다
(이론 피크는 16채널 × 4800 MT/s × 8 B = 614 GB/s). 따라서 아래의 "% of 상한" 은 실제보다
크게 잡힌 값이다.

**분자 — vLLM 추론 자체의 DRAM 소비**: Qwen3-14B, 6000 토큰 프롬프트 6개 동시,
GPU 사용률 100% 구간에서 20초 측정.

| 상태 | DRAM read | DRAM write | 합계 |
|---|---|---|---|
| 부하 중 (GPU 100%) | 7913 MB/s | 7893 MB/s | **15.8 GB/s** |
| 유휴 | 91 MB/s | 91 MB/s | 0.18 GB/s |

모델이 HBM 에 상주하므로 추론은 호스트 DRAM 을 거의 쓰지 않는다. (이 측정에서 LMCache
백엔드는 `DaxBackend`(CXL `/dev/dax0.0`)였고 CXL 트래픽은 `uncore_imc` 에 잡히지 않으므로,
15.8 GB/s 는 사실상 추론 자체의 몫이다.)

**합산**:

| 구성 | DRAM 소비 | COPY 상한(136.6) 대비 |
|---|---|---|
| 추론만 | 15.8 GB/s | 12% |
| 추론 + 스테이징 retrieval(35 GB/s × 1.9) | **~83 GB/s** | **60%** |
| 추론 + GPU-direct retrieval(20 GB/s × 0.23) | ~20 GB/s | 15% |

**결론: 이 구성에서 DRAM 절약은 실재하지만 결정적이지 않다.** 호스트 DRAM 은 포화와 거리가
멀다 — 추론이 여력의 12% 만 쓴다. 스테이징을 최대로 돌리면 60%(보수적 상한 기준, 이론
피크 기준으로는 14%)까지 올라가므로 지연 꼬리에 영향을 줄 수 있는 수준이지만 벽은 아니다.
GPU-direct 는 **부족하지 않은 자원의 여유를 늘리는 대가로 retrieval 대역폭 40% 를 내주는**
거래다. 단일 GPU 호스트에서는 값어치가 없다고 보는 것이 정직하다.

**다만 GPU 수에 선형으로 불리해진다(외삽).** 스테이징 트래픽은 GPU 당 발생하므로 GPU 4장이
동시에 retrieval 하면 ~268 GB/s 로 측정 상한을 넘고 이론 피크에도 근접한다. 그 지점에서는
GPU-direct 가 선택이 아니라 필요조건이 된다. 이 외삽은 측정하지 않았다 — 단일 GPU 장비뿐이라
검증할 수 없었다.

### 측정 이력과 정정

이 문서의 앞선 두 판은 잘못된 수치를 실었다. 원인은 매번 **컨테이너 구성** 이었다.

| 판 | 주장 | 실제 |
|---|---|---|
| 1판 | `pinnedcopy` 대비 대역폭 2.28×, 포화점 2.44×, CPU 1.86× 개선 | chunk 1 MiB 산물. 4 MiB 에서 소멸 |
| 2판 | 대역폭 이득 없음(12.05 vs 12.05), DRAM 만 남음 | `RP_2G1` 산물. 넓은 oclass 에서 스테이징이 25.6 으로 올라가 GPU-direct 가 오히려 짐 |
| 3판 | 대역폭·지연 열세(16W 0.59배), DRAM 만 우세 | cell1 이 NVMe 7개인 비대칭 구성. 대칭 8+8 에서 스테이징이 35.6 까지 올라가 격차가 더 벌어짐 |
| 현재 | 16W 0.68배·1W 0.52배 열세, DRAM 7.8~19× 우세 | 위 표 (대칭 8+8) |

교훈은 하나다. **스토리지 구성 기본값을 고정하지 않은 비교는 두 경로에 비대칭으로 작용한다.**
좁은 청크와 좁은 oclass 는 스테이징 경로를 훨씬 더 세게 때린다.

## 알려진 한계

- **측정 범위가 read 경로에 한정된다.** 대역폭·지연·cycles/byte·DRAM 트래픽·concurrency
  (1→32)는 측정했으나, **write 경로**(`dfs_write_gpu`)와 vLLM 수준의 TTFT 는 미측정이다.
- **GPU BAR 쓰기의 QP 당 제한을 DAOS 안에서 우회하는 방법을 찾지 못했다.** 원인은 위에서
  특정했지만(1 QP write 22 GB/s, 4 QP 39.9), DAOS/Mercury/UCX 가 하나의 포트에서 QP 를
  늘리도록 만드는 설정을 찾지 못했다. 그룹 폭 확대는 역효과였다. 프로세스 NUMA 고정
  효과도 미측정이다.
- **`daos_server` 재시작이 반복적으로 SPDK 를 wedge 시킨다.** 이 클러스터에서 재시작
  때마다 `device_unplugged` 와 `failed to init spdk context ... DER_NONEXIST` 가 재현되어,
  bdev_list 전체 wipe → `setup.sh reset` → 재기동 → format 을 거쳐야 복구된다. format 은
  풀을 파기하므로 **엔진 설정을 바꾸는 실험마다 풀 재구축 비용이 든다.** 이것이 서버 측
  전송 노브 실험을 막고 있는 실질적 장애물이다.
- **호스트별 `fabric_iface` 이름이 다르다** (cell1 `ens2`, cell2 `ens2np0`). 설정 파일을
  호스트 간에 그대로 복사하면 cell2 가 `can't determine device class for "ens2"` 로 기동
  실패한다. 실제로 한 번 그렇게 망가뜨렸다.
- **DRAM 이득의 값어치는 단일 GPU 에서만 확인했다.** 위에서 "결정적이지 않다" 는 결론을
  냈지만, 다중 GPU 호스트에서의 외삽(GPU 4장 → ~268 GB/s)은 장비가 없어 검증하지 못했다.
  그리고 DRAM 상한 자체가 단순 루프로 잰 하한 추정이므로, 실제 여력은 더 클 수 있다.
- **2엔진/호스트 토폴로지는 이 클러스터에서 불가능하다.** `provider: ucx+rc_v` 는 Mercury 가
  UCX TLS 를 `rc_v` 하나로 고정하게 만들고, 같은 호스트의 다른 엔진 주소는 `local` 라우팅
  테이블(우선순위 0)이 이겨 `dev lo` 로 해석되므로 RoCE 로 도달할 수 없다. `NA_UCX_EXTRA_TLS`
  로 `sm,self` 를 넣어도 na_ucx 가 sockaddr 로 엔드포인트를 만들기 때문에 `sm` 이 선택되지
  않는다. 실제로 시도했고 rank 가 SWIM 에 의해 배제됐다.
- **`load blobstore failed -1025` 은 장치 하나만 wipe 해서는 낫지 않는다.** 반복된 재구성
  뒤 이 오류가 나타났고, 실패하는 타깃이 재기동마다 옮겨다녔다. 문제 장치 하나만
  `blkdiscard` 하면 실패가 다른 타깃으로 이동할 뿐이었다. 복구된 절차는 **bdev_list 의 모든
  장치를 wipe → SPDK `setup.sh reset` → daos_server 재기동 → format** 이고, 이때 두 호스트를
  따로 처리해야 했다(한쪽은 reset 을 한 번 더 필요로 했다). SMART 는 전 과정에서 깨끗했으므로
  하드웨어 고장이 아니라 blobstore 메타데이터 상태 문제다.
- **rkey import 는 스텁이다.** `HG_Bulk_import_rkey()` 가 `HG_OPNOTSUPPORTED` 를 돌려주고
  호출자가 HMEM 등록으로 폴백한다. UCX 경로에서는 애초에 호출되지 않으므로 손실이 없지만,
  OFI/verbs + cuFile 조합을 쓰려면 진짜 구현이 필요하다.
- **proto v2 SEGV.** CUDA 메모리 타입이 탐지되지 않던 상태에서는 UCX proto-v2 의
  `ucp_wireup_replay_pending_request()` 에서 SEGV 가 났다. 위 패치들을 적용한 뒤에는
  재현되지 않는다 — 원인을 따로 규명한 것은 아니므로, 다시 나타나면
  `UCX_PROTO_ENABLE=n` 으로 격리한 뒤 이 항목을 다시 볼 것.
- **LMCache 연동이 없다.** 이 디렉터리는 DAOS 쪽 데이터 평면만 다룬다. `DaosGdsBackend`
  와 v2 저장 포맷은 별도 작업이다.
