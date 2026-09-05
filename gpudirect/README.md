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

**분모 — client-6 의 DRAM 대역폭** (2× Xeon 8558, DDR5-4800 × 16 DIMM, 1007 GB).

⚠️ **이 분모는 한 번 정정됐다.** 처음에는 `../tests/bench_dax_bw.c` 의 `anon` 모드(단순
언롤 루프, 64 스레드)로 재서 LOAD 214.8 / COPY **136.6 GB/s** 를 썼다. 그것은 메모리
시스템이 아니라 루프를 측정한 값이었다. `../tests/bench_dram_ceiling.c`(OpenMP 192 스레드,
32 GiB 배열, 채널 카운터로 교차검증)로 다시 재면 실제 상한은 **약 3배** 높다:

| 커널 | DRAM 트래픽 | 양 소켓 (192 thr) | 소켓0 고정 (96 thr) |
|---|---|---|---|
| read | 1R | 448.1 GB/s | 267.9 GB/s |
| **copy** | **1R+1W** | **401.7 GB/s** | **220.6 GB/s** |
| scale | 1R+1W | 420.5 | 230.7 |
| add | 2R+1W | 420.6 | 231.0 |
| triad | 2R+1W | 434.6 | 231.6 |

**`copy` 행이 분모다.** 스테이징은 한 바이트를 호스트 DRAM 에 넣고(NIC DMA write) 다시
꺼내므로(GPU 복사엔진 read) 읽기·쓰기가 반반이고, 그것이 `copy` 의 접근 패턴이다.

측정을 신뢰할 수 있는 근거는 **분자와 같은 계기로 쟀다**는 점이다 — 양쪽 모두
`perf stat -a -e uncore_imc/cas_count_{read,write}`. `read` 커널의 `imc/counted` 가
정확히 **1.00** 으로 나온 것이 계기 검증이다(저장이 없는 커널이므로 다른 값이 나올 수 없다).
`uncore_imc_0..7` × 2 소켓 = 16 채널이 실장 DIMM 16 개와 일치하는 것도 확인했다.

측정 과정에서 두 번 틀렸고 둘 다 스크립트에 방어 코드로 남겼다:
- 서로 다른 프로세스의 **wall clock 을 차분**했더니 `copy` 가 642 GB/s(이론 피크 614 초과),
  `read` 의 `imc/counted` 가 0.88(저장 없는 커널에서 불가능)로 나왔다. 지금은 **바이트만**
  차분하고 시간은 프로그램이 직접 잰 값을 쓴다.
- 4 GiB 배열에서는 L3 가 **520 MiB** 나 되어 12% 가 상주하고 `read` 가 0.91 로 낮게 나왔다.
  32 GiB 로 키워 1.6% 로 낮췄다.

저장 커널의 `imc/counted` 가 고전적인 1.50(read-for-ownership)이 아니라 1.04~1.11 인 것은
정상이다. 바이너리에 비시간적 저장이 없음을 확인했고(`objdump`: `movnt` 0건), Emerald
Rapids 가 순차 full-line write 의 소유권 읽기를 생략하기 때문이다.

**분자 — vLLM 추론 자체의 DRAM 소비**: Qwen3-14B, 6000 토큰 프롬프트 6개 동시,
GPU 사용률 100% 구간에서 20초 측정.

| 상태 | DRAM read | DRAM write | 합계 |
|---|---|---|---|
| 부하 중 (GPU 100%) | 7913 MB/s | 7893 MB/s | **15.8 GB/s** |
| 유휴 | 91 MB/s | 91 MB/s | 0.18 GB/s |

모델이 HBM 에 상주하므로 추론은 호스트 DRAM 을 거의 쓰지 않는다. (이 측정에서 LMCache
백엔드는 `DaxBackend`(CXL `/dev/dax0.0`)였고 CXL 트래픽은 `uncore_imc` 에 잡히지 않으므로,
15.8 GB/s 는 사실상 추론 자체의 몫이다.)

**합산** (분모 = `copy` 401.7 GB/s):

| 구성 | DRAM 소비 | 상한 대비 |
|---|---|---|
| 추론만 (1 GPU) | 15.8 GB/s | 4% |
| 추론 + 스테이징 retrieval(35 GB/s × 1.9) | ~82 GB/s | 20% |
| 추론 + GPU-direct retrieval(20 GB/s × 0.23) | ~20 GB/s | 5% |

**결론: 단일 GPU 호스트에서 DRAM 절약은 값어치가 없다.** 분모를 제대로 재고 나니 이전 판의
"60%" 가 20% 로 내려갔다. 스테이징을 최대로 돌려도 호스트 DRAM 은 여력의 1/5 만 쓴다.
GPU-direct 는 **부족하지 않은 자원의 여유를 늘리는 대가로 retrieval 대역폭 40% 를 내주는**
거래다. 이 방향의 결론은 이전 판과 같고, 근거는 더 강해졌다(여유가 이전 추정보다 3배 크다).

#### 교차점: 스테이징이 DRAM 벽에 닿는 지점

분모가 3배로 커졌으므로 "GPU 가 늘면 필연" 이라던 이전 외삽도 다시 계산해야 한다. 남은
DRAM 여력을 스테이징 배수로 나누면 스테이징이 감당할 수 있는 **집계** retrieval 대역폭이
나온다:

| 시나리오 | DRAM 상한 | 추론 몫 | 스테이징 여력 | 스테이징 집계 한계 | GPU 8장 시 GPU 당 |
|---|---|---|---|---|---|
| 버퍼가 두 소켓에 분산 | 401.7 | 126 (8×15.8) | 276 | **138 GB/s** | 17.3 GB/s |
| 버퍼가 NIC 소켓에 집중 | 220.6 | 63 (절반) | 158 | **79 GB/s** | 9.9 GB/s |

GPU-direct 쪽은 같은 계산에서 DRAM 에 걸리지 않는다(276 / 0.23 = 1200 GB/s). 대신 §6-3 의
QP 당 22 GB/s 제한에 걸려 **8 프로세스 × 22 = 176 GB/s** 가 상한이다.

즉 **교차점은 집계 138 GB/s(GPU 당 17.3)** 이다. 그 아래에서는 스테이징이, 위에서는
GPU-direct 가 이긴다. 버퍼가 한 소켓에 몰리면 교차점이 79 GB/s(GPU 당 9.9)로 내려간다.

**이것이 실제로 GPU 8장에서 어느 쪽인지는 스토리지 티어가 함께 커지는지에 달렸다.**

| 스토리지 티어 | 집계 retrieval | 스테이징 | 판정 |
|---|---|---|---|
| 지금 그대로 (DAOS 2노드, 집계 ~37 GB/s) | 37 | 74 GB/s DRAM (18%) | 스테이징으로 충분. **GDS 불필요** |
| GPU 당 ~17 GB/s 이상 공급 (≈4노드+) | 138+ | 여력 소진 | **GDS 필요** |
| GPU 당 측정치 35 GB/s 를 그대로 (≈8노드) | 280 | 560 GB/s 요구 > 401.7 | 스테이징 **불가능** |

이전 판은 "GPU 4장이면 ~268 GB/s 로 상한 초과" 라고 썼는데, 그 계산은 **GPU 당 35 GB/s** 를
가정한 것이었다. 이 클러스터의 35 GB/s 는 집계값이므로 GPU 를 늘려도 스토리지가 그대로면
집계는 늘지 않는다 — 그 외삽은 per-GPU 와 집계를 섞은 오류였다. 정정한다.

정직한 요약: **GPU 8장이라는 조건만으로는 GDS 가 정당화되지 않는다. 스토리지 티어가 GPU 당
약 17 GB/s 이상을 공급할 때 정당화된다.** 다만 그 조건은 8장 배포에서 자연스럽다 — 그보다
적게 공급하면 KV 티어가 병목이 되어 GPU 가 굶기 때문이다.

#### 스테이징 버퍼는 실제로 한 소켓에 몰려 있고, 그것은 공짜로 고칠 수 있다

위 표의 두 행 중 어느 쪽인지를 쟀다. `perf stat -a --per-socket` 으로 소켓별 IMC 를
분리해, 같은 `pinnedcopy` 부하(16 워커, 8 GiB, chunk 32 MiB, `crp2g4`)를 배치 정책만
바꿔 세 번 돌렸다.

| 배치 | 대역폭 | S0 (MiB) | S1 (MiB) | 합계 B/B | 편중 |
|---|---|---|---|---|---|
| 기본 (배치 지정 없음) | 34.77 GB/s | 2702 | 11461 | 1.73 | **S1 80.9%** |
| `numactl --interleave=all` | **35.46 GB/s** | 6903 | 7409 | 1.75 | S1 51.8% |
| `numactl -N0 -m0` | 34.16 GB/s | 12894 | 257 | 1.61 | S0 98.0% |

기본 상태에서 스테이징 DRAM 트래픽의 **81% 가 소켓1 에 몰린다.** 이 호스트는 SNC 가 꺼져
있고 NIC 과 GPU 가 **둘 다 소켓0** 인데, 버퍼는 반대쪽 소켓에 잡히고 있다 — 스테이징된
모든 바이트가 UPI 를 건너간다는 뜻이다.

`--interleave=all` 로 균등하게 펴면 편중이 52% 로 내려가고, **대역폭은 오히려 조금 올라간다**
(34.77 → 35.46). 즉 스테이징의 실효 DRAM 상한을 소켓 하나(220.6)에서 노드 전체(401.7)로
끌어올리는 데 드는 비용이 0 이다. `-N0 -m0` 로 NIC·GPU 쪽에 붙이면 총 트래픽은 줄지만
(1.73 → 1.61 B/B, UPI 왕복이 없어져서) 한 소켓에 98% 가 몰려 상한은 가장 낮아진다.

배치를 바꿔도 대역폭이 34~35 GB/s 로 거의 같은 것은 예상대로다 — 이 구간의 병목은 DRAM 이
아니라 스토리지 클러스터다.

**측정된 편중으로 교차점을 다시 계산하면:**

| 배치 | 적용 상한 | 추론 몫 | 스테이징 배수(편중 반영) | 집계 교차점 | GPU 당 |
|---|---|---|---|---|---|
| 기본 (S1 81%) | 220.6 (S1) | 63 | 1.73 × 0.809 = 1.40 | **113 GB/s** | 14.1 GB/s |
| `--interleave=all` | 401.7 (노드) | 126 | 1.75 | **158 GB/s** | **19.7 GB/s** |
| `-N0 -m0` (S0 98%) | 220.6 (S0) | 63 | 1.61 × 0.98 = 1.58 | 100 GB/s | 12.5 GB/s |

**이것이 GDS 논거를 한 번 더 좁힌다.** 인터리브만 켜면 스테이징이 GPU 당 19.7 GB/s 까지
버티는데, GPU-direct 자신의 상한이 §6-3 의 QP 당 **22 GB/s** 다. 즉 **GDS 가 대역폭으로
이기는 구간은 GPU 당 19.7 ~ 22 GB/s 라는 좁은 띠뿐이다.** 그 위로는 GDS 도 QP 제한에
걸려 못 준다(QP 수를 늘릴 방법을 찾지 못했다).

같은 방법으로 잰 `gpu` arm 은 S0 1092 / S1 964 MiB, 합계 **0.251 B/B** 로 편중이 없다
(S0 53%). 페이로드가 호스트 DRAM 을 지나지 않으므로 남은 것이 제어 트래픽뿐이고,
그래서 배치와 무관하다.

남은 미검증 항목 하나: 8-GPU 추론의 DRAM 소비를 1-GPU 15.8 GB/s 의 선형 외삽으로 잡았다
(고정 오버헤드가 있으면 과대평가이고, 그러면 교차점은 더 올라간다 = GDS 에 더 불리하다).

### 지연 이득 측정 (Phase B): GDS 는 실제 chunk 크기에서 더 느리다

DRAM 논거가 사라진 뒤 남은 근거는 지연이었다 — 스테이징은 전송 후 H2D 복사를 직렬로 더
하고, 그 몫은 인터리브로도 없어지지 않는다. 그것을 쟀다. 도구는
`../tests/bench_dfs_gpu_lat.c` 로, 기존 처리량 벤치와 두 가지가 다르다:

1. chunk 마다 개별 타이밍을 남기고 분포를 보고한다(처리량 벤치의 `chunk_ms` 는 전체
   시간 ÷ chunk 수라서, 워커가 여러 개면 한 스레드의 복사가 다른 스레드의 전송 아래
   숨는다 — 포화 처리량에는 맞고 TTFT 에는 틀린 값이다).
2. **같은 chunk 안에서 전송과 복사를 분리해 잰다** (`t0 → dfs_read → t1 → cuMemcpyHtoD
   → t2`). 두 실행을 차분하는 것이 아니라 직접 측정한다.

분리의 타당성은 `pinned` arm(같은 읽기, 복사 없음)으로 교차검증했다. `pinnedcopy` 평균 −
`pinned` 평균 = 83~101 µs 이고 직접 측정한 `copy_p50` = 83.2 µs 로 일치한다. 그리고 이
도구는 처리량 벤치와 겹치는 지점에서 같은 값을 낸다(스테이징 35.08 vs 35.00 GB/s,
gpu 16.74@16MiB vs 17.92@32MiB) — 두 독립 도구의 교차검증이다.

#### 복사 자체는 지연의 작은 몫이다

| chunk | `copy_p50` | 함의 H2D | `pinnedcopy` chunk 지연 대비 |
|---|---|---|---|
| 256 KiB | 12.4 µs | 21.1 GB/s | 2.5% |
| 1 MiB | 27.2 µs | 38.5 GB/s | 4.5% |
| 4 MiB | 83.3 µs | 50.4 GB/s | 6.9% |
| 16 MiB | 308.7 µs | 54.4 GB/s | 14.9% |

고정 오버헤드 ~9 µs, 점근 54 GB/s(PCIe Gen5 x16)로 일관된다. **즉 "스테이징 복사를
없앤다" 는 논거로 벌 수 있는 최대치가 chunk 지연의 2.5~15% 다.** 이것이 지연 논거의
상한이며, 추정이 아니라 측정이다.

#### 실제 chunk 크기에서는 GDS 가 2배 느리다

작업집합 전체를 가져오는 데 걸린 시간(`total_ms`)이 판정 지표다. 대형 chunk 는 표본 수를
확보하기 위해 8 GiB 작업집합으로 다시 쟀다.

| chunk | 워커 | pinned | pinnedcopy | gpu | gpu / pinnedcopy |
|---|---|---|---|---|---|
| 256 KiB | 1 | 1951 | 2003 | **1173** | **0.59** |
| 256 KiB | 16 | 482 | 487 | **176** | **0.36** |
| 1 MiB | 1 | 582 | 622 | 631 | 1.01 |
| 1 MiB | 16 | 128 | 123 | 126 | 1.02 |
| 4 MiB | 1 | 2312 | 2483 | 2790 | 1.12 |
| 4 MiB | 4 | 711 | 762 | 730 | 0.96 |
| 4 MiB | 16 | 313 | 323 | **610** | **1.89** |
| 16 MiB | 1 | 910 | 1064 | 1003 | 0.94 |
| 16 MiB | 4 | 367 | 386 | **737** | **1.91** |
| 16 MiB | 16 | 242 | 245 | **513** | **2.09** |

**최적 대 최적으로 스테이징이 2.1배 빠르다** (16 MiB/16W: 245 vs 513 ms).

이 표에서 GDS 가 이기는 곳은 **256 KiB 하나뿐**이고 거기서는 크게 이긴다(0.36~0.59배).
그런데 그 이득은 복사 제거로 설명되지 않는다 — 256 KiB 에서 복사는 chunk 지연의 2.5%
인데 격차는 41~64% 다. `host`/`hostcopy` 대조군도 원인이 아니다: 256 KiB/1W 에서
`host` 2243 > `pinned` 1951 이므로 pinned 할당이 호스트 경로를 느리게 만드는 것이 아니고,
`pinnedcopy` 가 이미 호스트 변형 중 최선이다. 남는 것은 **호스트 읽기 경로 자체가 작은
전송에서 chunk 당 ~190 µs 를 더 쓴다**는 사실이다(256 KiB/1W: gpu 286 vs pinned 476 µs).
원인은 특정하지 못했다 — 소형 전송에서 프로토콜 선택이 갈리는 것으로 의심되지만
확인하지 않았다.

#### 그런데 256 KiB 는 LMCache 의 실제 chunk 가 아니다

Qwen3-14B 는 layers 40, KV heads 8, head_dim 128, bf16 이므로 **토큰당 KV = 160 KiB** 다.
LMCache 기본 `chunk_size` 256 토큰 → **chunk 40 MiB**. 256 KiB 는 1.6 토큰에 해당하므로
설정으로 도달할 수 있는 영역이 아니다(prefix 매칭 단위와 메타데이터 효율이 붕괴한다).

6000 토큰 프롬프트 하나의 KV = 937 MiB 이므로 위 표를 TTFT 로 환산하면:

| 경로 | 최적 대역폭 | 937 MiB 인출 | TTFT 영향 |
|---|---|---|---|
| 스테이징 (16 MiB/16W) | 35.08 GB/s | **28.0 ms** | 기준 |
| GDS (16 MiB/16W) | 16.74 GB/s | **58.7 ms** | **+30.7 ms** |
| 스테이징에서 복사를 완벽히 제거했다면 | — | 25.2 ms | −2.8 ms |

즉 복사를 완벽히 없애서 벌 수 있는 것이 2.8 ms 인데, GDS 로 바꾸면 30.7 ms 를 잃는다.
원인은 이미 §6-3 에서 특정한 QP 당 GPU BAR 쓰기 제한이고, DAOS 안에서 QP 를 늘리는 방법을
찾지 못했다.

**Phase B 판정: 부정. `DaosGdsBackend` 를 만들지 않는다.** 지연이 마지막 근거였고, 그
근거가 측정으로 반대 방향임이 확인됐다.

#### 256 KiB 이득은 GPU-direct 의 이득이 아니었다 (추가 조사)

위에서 "복사로 설명되지 않는 chunk 당 ~190 µs" 를 `DaosConnector` 개선 여지로 남겼다.
조사했고, **그 해석이 틀렸다.** 64 KiB~4 MiB 를 촘촘히 재면:

| chunk | pinned | gpu | pinned p95 |
|---|---|---|---|
| 64 KiB | 136.2 | 155.3 | 152.3 (좁음) |
| 128 KiB | 199.7 | 218.1 | 213.9 (좁음) |
| **256 KiB** | **483.7** | **290.6** | 676.1 (이봉) |
| 512 KiB | 522.2 | 565.6 | 714.8 |
| 1 MiB | 580.4 | 619.1 | 759.2 |
| 4 MiB | 1201.5 | 1229.6 | 1315.8 |

**64·128 KiB 에서는 GPU 경로가 오히려 느리다.** 호스트 경로는 128→256 KiB 에서 200→484 µs
로 튀고 그 지점부터 이봉 분포가 되며, GPU 경로는 256 KiB 까지 좁게 유지되다가 512 KiB 에서
튄다. 즉 **256 KiB 는 서로 다른 두 임계 사이의 틈**일 뿐이고, GPU-direct 의 구조적 이득이
아니다. Phase B 의 부정 판정을 약화시키는 것이 아니라 강화한다.

원인 절반을 특정했다. `daos_mem_type_t` 에 `DAOS_MEM_TYPE_HOST = 0` 이 있으므로,
`dfs_read_gpu()` 를 **pinned 호스트 버퍼**로 호출하는 arm(`hostattr`)을 만들어 갈랐다:

| arm | 진입점 | 목적지 | 256 KiB chunk 지연 |
|---|---|---|---|
| `pinned` | `dfs_read` | 호스트 | 478.0 µs |
| `hostattr` | `dfs_read_gpu` | 호스트 | **486.5 µs** |
| `gpu` | `dfs_read_gpu` | GPU | **291.5 µs** |

**진입점이 아니라 목적지 메모리 타입이 원인이다.** `dfs_read` 와 `dfs_read_gpu` 는 DFS·array
API 계층에서 `args->mem_attr` 하나만 다른 동일 코드이므로(소스 확인), 차이는 전송 계층의
메모리 타입별 동작이다. 클라이언트측 UCX 노브로는 재현되지 않았다 —
`UCX_RNDV_FRAG_SIZE=host:4M`, `UCX_RNDV_THRESH=inf`, `UCX_RNDV_SCHEME=get_zcopy` 모두
478~485 µs 로 변화 없음. DAOS fetch 의 bulk 는 **서버가 개시**하므로(`CRT_BULK_PUT`)
클라이언트 설정이 결정권을 갖지 않는 것과 일치한다. 서버측 확인은 `daos_server` 재시작이
SPDK 를 wedge 시키는 위 문제로 막혀 있다.

**결론: 실제 chunk 크기(28~40 MiB)에서는 호스트 경로가 더 빠르므로 운영에 영향이 없다.**
`DaosConnector` 개선 항목에서 내린다.

### 인터리브 적용 (Phase A): 적용·검증 완료

`deploy/launchers/run_vllm_daos.sh` 에 `numactl --interleave=all` 을 넣었다. 컨테이너
이미지에 `numactl` 이 있다. `--cpuset-mems` 로 대체하면 안 된다 — 사용 가능한 노드만
제한하고 기본 local 정책은 그대로여서 인터리브가 되지 않는다.

검증 (client-6, 실제 vLLM 경로):
- 정책 활성: vLLM 프로세스의 매핑 2478 줄 중 **2381 줄이 `interleave`**
- 리트리브 중 소켓별 DRAM: S0 2267 MiB / S1 2180 MiB = **51.0% / 49.0%** (균형).
  벤치의 인터리브 케이스(52/48)와 일치하고 기본 케이스(81/19)와 다르다.

⚠️ 운영 경로의 **before 측정은 없다.** 이 컨테이너의 LMCache 설정이
`plugin://daos/kvpool2/kv2s16` 를 가리키고 있었고 `kvpool2` 는 이미 파기되어
(`DER_NONEXIST`) DAOS 백엔드가 죽은 상태였기 때문이다. 그래서 "운영 경로가 81/19 에서
51/49 로 바뀌었다" 고는 말할 수 없다. 말할 수 있는 것은 정책이 켜졌고 결과 트래픽이
균형이라는 것이다. 새 컨테이너 `gdspool/kvlmc` 는 벤치와 동일한 속성으로 만들었다
(`daos fs get-attr` 로 file oclass `RP_2G4`, chunk `4194304`, dir oclass 까지 일치 확인).

### 그런데 운영 경로의 병목은 전송이 아니다

위 검증에서 나온 LMCache 자체 계측이 이 문서 전체의 결론을 확정한다.
Qwen3-1.7B, 6000 토큰 프롬프트(5888 토큰 저장, 0.6289 GB):

```
Stored    ... cost 93.4513 ms, throughput 6.7298 GB/s;
              offload_time: 93.2829 ms, put_time: 0.1324 ms
Retrieved ... cost 181.7296 ms, throughput 3.4607 GB/s
```

⚠️ **정정.** 이 문서의 앞 판은 위 수치를 "DAOS 쓰기 0.13 ms vs offload 93.28 ms = 약 700배"
라고 읽었다. **틀렸다.** LMCache 소스를 확인하면(`cache_engine.py`) 두 타이머는 이렇다:

```python
offload_time = (store_stats.process_tokens_time + store_stats.from_gpu_time)
with store_stats.profile_put():
    self.storage_manager.batched_put(...)      # <- put_time 이 감싸는 것
```

`batched_put()` 은 **비동기 제출**이다. 즉 `put_time` 0.13 ms 는 큐에 넣는 비용이고
**실제 DAOS 쓰기 시간을 포함하지 않는다.** 따라서 이 수치로 말할 수 있는 것은
"스토리지가 700배 빠르다" 가 아니라 **"store 경로에서 스토리지 쓰기는 동기 임계경로 밖에
있다"** 는 것뿐이다. 700배 비교는 철회한다.

리트리브 쪽은 다르다. prefill 전에 토큰이 실제로 적재되어야 하므로 **동기**이고,
`cost 181.7296 ms, throughput 3.4607 GB/s` 는 DAOS 읽기를 포함한 진짜 end-to-end 값이다.
같은 하드웨어의 raw DFS 스테이징이 35 GB/s 인데 여기서는 3.46 GB/s — **1/10** 이다.
이 차이는 DAOS 읽기 밖의 몫(디시리얼라이즈, H2D, GIL, 파이프라인)에서 나온다.

즉 운영 경로에서 **전송 계층을 GPU-direct 로 바꿔 줄일 수 있는 몫은 전체의 일부에
불과하다.** Phase B 의 판정과 같은 방향이며, 리트리브의 10배 격차가 그 근거다.
아래에서 그 격차를 실제로 분해했다.

#### 분해 결과: 99% 가 GPU⇄CPU 복사이고, 그 대부분은 배칭 부재다

LMCache 는 store 를 `offload_time` 하나로만 찍고 retrieve 는 분해를 아예 찍지 않는다.
그런데 타이머 자체는 이미 존재한다(`process_tokens_time`, `from_gpu_time`, `to_gpu_time`,
`broadcast_time`). 그래서 그것들을 출력하도록 두 파일을 패치해 읽었다
(`../tests/patch_lmcache_timers.py`, 앵커 5개 전부 fail-closed 검사). py-spy 는 쓸 수
없었다 — vLLM 의 스레드 수가 많아 5초 창에서 **128초 뒤처졌고**, 측정 대상 구간이 60~160 ms
라 표본이 잡히지 않는다.

Qwen3-1.7B, 6000 토큰 프롬프트, 0.6289 GB, 8회 반복(store 첫 회는 워밍업이라 제외):

| 단계 | store | retrieve |
|---|---|---|
| `process_tokens` (해싱·청킹·할당) | 0.50 ms (**0.8%**) | 0.22 ms (**0.13%**) |
| **GPU⇄CPU 복사** (`from_gpu`/`to_gpu`) | **61.0 ms (99.2%)** | **163.9 ms (99.7%)** |
| `broadcast` | — | 0.00 ms |
| `put` (비동기 제출) | 0.11 ms | — |
| 합계 | 61.5 ms | 164.5 ms |
| 실효 대역폭 | 10.3 GB/s (D2H) | **3.84 GB/s (H2D)** |

**토큰 처리는 사실상 0 이다.** 착수 전 "해싱·직렬화가 지배할 것" 이라는 예상은 틀렸다 —
0.8% / 0.13% 다. 시간은 전부 vLLM paged KV ⇄ 연속 버퍼 이동에 있다.

원인은 소스에 그대로 있다 (`v1/gpu_connector/gpu_connectors.py`):

```python
def batched_to_gpu(self, memory_objs, starts, ends, **kwargs):
    for memory_obj, start, end in zip(memory_objs, starts, ends, strict=False):
        self.to_gpu(memory_obj, start, end, **kwargs)

# TODO(Yuwei): need to optimize to enable real batching
def batched_from_gpu(self, memory_objs, starts, ends, **kwargs):
    for memory_obj, start, end in zip(memory_objs, starts, ends, strict=False):
        self.from_gpu(memory_obj, start, end, **kwargs)
```

**`batched_*` 는 이름만 배칭이고 실제로는 chunk 단위 Python 루프다.** LMCache 자신의 TODO
가 그렇게 적어놨다. 5888 토큰 / chunk 256 = 23 chunk 이므로 retrieve 는 chunk 당 7.1 ms 다
(chunk 28 MiB → 3.9 GB/s).

#### 이 분해가 GDS 에 주는 함의: Phase B 판정이 유지된다

`to_gpu` 는 0.6289 GB 를 PCIe 로 넘겨야 한다. 이 호스트에서 측정한 H2D 점근 대역폭은
54 GB/s 이므로 **순수 데이터 이동의 하한은 11.6 ms** 다. 관측된 `to_gpu` 는 163.9 ms 이므로:

- PCIe 데이터 이동: **≤ 7.1%**
- 나머지 **≥ 92.9%**: chunk 당 고정비용(Python 루프, 커널 런치, 동기화, scatter 입도)

`DaosGdsBackend` 가 MemoryObj 를 GPU 상주로 만들면 PCIe 몫(≤7.1%)이 사라지고 scatter 입도
손해의 일부가 개선될 수 있다. 그러나 **Python 루프와 커널 런치 오버헤드는 목적지를 바꿔도
남는다.** 즉 지배 항목이 스토리지도 전송도 아니고 GDS 로 제거되지도 않는다.

**따라서 최우선 개선은 LMCache 의 `batched_*` 배칭이고, 그것은 스토리지와 무관하다.**
Phase B 의 부정 판정은 유지된다 — 근거가 하나 더 늘었다.

(부수 관찰: 24개 프롬프트를 연속 투입했을 때 LMCache 가
`Ref count of MemoryObj ... is negative: -1. Double free occurred somewhere` 경고를 다수
출력했다. LMCache 자체 버그이고 이 작업 범위 밖이지만 부하 시 재현되므로 기록해둔다.)

**따라서 다음 작업은 스토리지 쪽이 아니라 LMCache offload 경로다.**

## 🔴 정합성 버그: 동시 읽기에서 4 MiB chunk 하나가 조용히 깨진다

배칭 작업에 착수하려다 두 개의 선행 문제를 발견했고, 두 번째가 심각하다.

### 발견 경로

`batched_*` 최적화 전에 기준선을 확인하려고 **생성 결과 동일성 게이트**를 만들었다
(`../tests/kv_correctness_gate.sh`). 같은 프롬프트를 temperature 0 으로 두 번 보내
(1회차=계산·저장, 2회차=캐시 적재) 토큰이 같은지 본다. 결과:

| 구성 | 결과 |
|---|---|
| LMCache `LocalCPUBackend` (DAOS 없음) | A=B1=B2=B3, 일관·정확 |
| **DAOS 백엔드 (`enable_async_loading: True`)** | **A≠B, 그리고 B1≠B2≠B3** |
| **DAOS 백엔드 (`enable_async_loading: False`)** | **동일하게 실패** |

즉 엔진은 결정적이고 LMCache 로컬 경로는 정확하며, **DAOS 경로만 틀린 값을 돌려주고
그 값이 매 호출마다 다르다.** async 로딩을 꺼도 재현되므로 async 경로 문제가 아니다.

### 하위 계층에서 재현: 원인은 4 MiB chunk 경계다

LMCache 를 배제하고 DFS 바인딩만 시험했다 (`../tests/test_rawio_integrity.py`,
28 MiB × 16 스레드, 커넥터와 동일한 aliased ctypes 버퍼 + 헤더/페이로드 2회 오프셋 쓰기):

```
t2 r0: MISMATCH at byte 12582876, 1024/7168 sampled pages differ
t3 r0: MISMATCH at byte 20971484, 1024/7168 sampled pages differ
t4 r0: MISMATCH at byte  4194268, 1024/7168 sampled pages differ
```

오프셋이 결정적이다. 헤더가 **36 B**(prefix 8 + meta 28)이므로 페이로드 위치 `p` 의 파일
오프셋은 `p+36` 이고:

| 페이로드 위치 | +36 = 파일 오프셋 |
|---|---|
| 4194268 | **4 MiB** |
| 8388572 | **8 MiB** |
| 12582876 | **12 MiB** |
| 16777180 | **16 MiB** |
| 20971484 | **20 MiB** |

**모든 손상이 정확히 4 MiB 파일 오프셋 경계에서 시작한다** — 컨테이너의 DFS chunk
크기(`4194304`)와 일치한다. 그리고 매번 **정확히 4 MiB 한 덩어리**만 깨진다
(7168개 샘플 페이지 중 1024개 = 4 MiB).

### 동시성 의존이다

| 스레드 | 결과 |
|---|---|
| 1 | PASS |
| 2 | PASS |
| **4** | **FAIL** |
| **16** | **FAIL (9/48 사이클)** |

**커넥터는 기본 16 워커로 동작한다**(`self._workers = 16`). 즉 이 버그는 정상 운영
조건에서 재현된다.

### 성격과 영향

- **조용하다.** 크기는 맞고(`got == payload_len`), `Retrieved 5888 out of 5888` 이 정상
  출력되고, 오류 로그가 없다. 생성은 계속 유창해서 눈에 띄지 않는다.
- **헤더 36 B 때문에 모든 페이로드 전송이 chunk 경계에 대해 비정렬**이다. 즉 4 MiB 를
  넘는 모든 KV chunk 가 다중 chunk straddling 경로를 탄다.
- 이 문서의 리트리브 **처리량** 수치(3.46~3.86 GB/s)는 바이트 양은 옮겼으므로 대체로
  유효하지만, **정확한 구현의 비용이라고는 말할 수 없다.** 재측정이 필요하다.

### 원인 1 확정: **내가 넣은 mercury 패치**다 (4 MiB 정렬은 원인이 아니었다)

처음에 "36 B 헤더 때문에 4 MiB chunk 경계를 straddle 해서" 라고 진단하고 정렬 수정을
권했다. **그 진단은 틀렸다.** 페이로드 오프셋을 바꿔 재보면:

| 페이로드 오프셋 | 결과 | 최초 불일치 |
|---|---|---|
| 36 (straddling) | FAIL | 16777180 = 16 MiB − 36 |
| 0 (chunk 정렬) | **FAIL** | 20971520 = 정확히 20 MiB |
| 4 MiB (chunk 정렬) | **FAIL, 더 심함** | 0, 4 MiB, 8 MiB, 20 MiB |

정렬해도 깨진다. 게다가 한 사례에서 **스레드 14가 스레드 15의 패턴을 읽었다** — 정렬
문제가 아니라 동시성 문제라는 신호다. 헤더는 경계가 나타나는 *위치*만 옮겼다.

진짜 원인은 라이브러리 경로를 바꿔가며 좁혔다. 동일 호스트·동일 코드·동일 번들에서
`LD_LIBRARY_PATH` 만 바꾸자 재현됐고, 최종적으로:

| 조건 (그 외 전부 동일) | 결과 |
|---|---|
| CUDA 경로 선두 + 패치 기본값 | **FAIL** |
| CUDA 경로 선두 + `NA_UCX_EXTRA_TLS=` | **PASS 80/80** |
| DAOS 경로 선두 (CUDA 뒤) | **PASS 80/80** |

`patches/mercury-0001-keep-cuda-memtype-tls.patch` 가 UCX TLS 에
`cuda_copy,cuda_ipc` 를 **기본 ON** 으로 추가한다. 그 패치는 `dfs_read_gpu()` 가 GPU
메모리를 등록할 수 있게 하려고 내가 넣은 것이다. CUDA 가 로드 가능하면(이 컨테이너는
항상 그렇다 — `LD_LIBRARY_PATH` 선두가 CUDA) UCX 가 그 메모리 타입 컴포넌트를 올리고,
그 상태에서 **일반 호스트 메모리 bulk 전송**이 깨진다. 전송당 4 MiB 한 덩어리, 동시
독자 4개 이상에서만, 크기·반환코드는 정상.

즉 **GPU-direct 를 켜기 위한 패치가 GPU 를 쓰지 않는 경로를 조용히 깨뜨렸다.** Phase B
에서 GPU-direct 를 만들지 않기로 결론냈으므로 이 패치는 기본 OFF 여야 한다(opt-in).

적용한 조치:
- 런처에 `-e NA_UCX_EXTRA_TLS=` (현재 설치된 빌드에 대한 런타임 해제). 정리 대상이 아니다.
- 라이브러리 순서로 우회하지 말 것. DAOS 선두도 통과하지만 그건 UCX 가 CUDA 를 아예
  못 찾아서 생기는 우연이다.

검증: 컨테이너 안에서 `test_rawio_integrity.py 28 16 5` **PASS 80/80**.

### ⚠️ 원인 2는 아직 남아 있다 (DFS 계층 위)

**raw DFS I/O 는 고쳐졌지만 end-to-end 게이트는 여전히 실패한다** (`match=0 mismatch=4`).
바이트는 정확히 오가는데 vLLM 이 복원한 KV 는 여전히 생성을 바꾼다. 즉 **DFS 위 계층에
독립적인 두 번째 문제**가 있다. `LocalCPUBackend` 는 같은 게이트를 통과하므로 LMCache
공통 경로가 아니라 **DAOS remote 백엔드 경로**다.

가장 유력한 가설 — **store 측 alias 경합**: `_prep_write()` 는 MemoryObj 버퍼를
복사하지 않고 alias 한다(의도된 최적화, 저장 오버헤드 5178→65 ms). 그런데 LMCache 의
`batched_put()` 은 비동기 제출이므로, 우리 쓰기가 끝나기 전에 LMCache 가 MemoryObj 를
`ref_count_down` 하고 재사용하면 우리는 이미 다른 내용이 된 버퍼를 저장한다. 앞서
관측한 `Ref count of MemoryObj ... negative: -1. Double free occurred somewhere` 경고가
이 가설과 맞는다.

#### 실패율: 계측을 고치고 나니 85% 다 (앞선 모든 비율 수치는 무효)

아래 절들에 실패율 10% / 85% / 100% / 65% / 75% 를 적었다. **전부 무효다.** 각각 최소
하나의 계측 결함이 있었고, 결함을 하나씩 제거한 뒤의 유효한 수치는 이것뿐이다:

**copy-store + `NA_UCX_EXTRA_TLS=` 구성, 20 시행: 실패 17건 = 85%, 95% CI [64.0%, 94.8%]**
(전부 read-side, store-side 0, invalid 0, unsettled 0, 컨테이너 시작 0 → 종료 380 객체)

제거한 결함 세 개:

1. **프롬프트가 실행 간 동일했다.** `rate{k}` + 고정 산문이라 매 실행 같은 키였다. 그래서
   pass A 가 **이전 실행이 남긴 객체를 히트**했고, A 와 B 가 *같은 손상 데이터*를 돌려주면
   일치로 집계됐다. **10% 라는 낮은 수치의 정체가 이것이다 — 손상끼리 비교한 것.**
   지금은 실행별 nonce 를 넣고, A 에서 히트 증가가 있으면 그 시행을 버린다.
2. **컨테이너가 초기화되지 않았다.** `daos cont destroy` 를 vLLM 정지 *전에* 실행해
   조용히 실패했다. 두 셀 모두 "objects at start: 380" 이 그 증거다. 순서를 고쳤다.
3. **store 완료 검사가 비침습적이 아니었다.** `daos_store_quiesce.py` 가 매 시행
   **60초 타임아웃까지** 돌면서(즉 완료 판정에 실패하면서) 두 번째 DAOS 클라이언트로
   컨테이너 전체를 읽었다. 85% 를 처음 만든 그 수치는 계측 부하의 결과였다.

##### 그래서 인과 주장을 다시 내린다

alias 100% [83.9, 100] 대 copy 85% [64.0, 94.8] — **신뢰구간이 겹친다.** 게다가 alias
측정도 결함 1을 안고 있었다. 따라서 **store alias 가 원인이라는 주장은 성립하지 않는다.**
이 하위 질문에서 나는 주장→철회→재주장을 반복했으므로, 이번에는 결론을 내지 않고 상태만
적는다: **copy 는 alias 대비 유의한 개선이 확인되지 않았다.** 기본값으로는 유지한다
(원리적 위험 제거, 측정 가능한 비용 없음) — 그 이상은 주장하지 않는다.

##### 지금 확실한 것

| 계층 | 상태 |
|---|---|
| raw DFS I/O (16 스레드, 28 MiB, 반복) | **PASS 80/80** — 바이트 정확 |
| end-to-end KV (vLLM 생성 동일성) | **85% 실패** |

**바이트는 DFS 계층에서 정확한데 end-to-end 는 깨진다.** 따라서 원인은 전송·스토리지가
아니라 **커넥터의 DFS 사용 방식 또는 LMCache 통합**에 있다. 이것은 이제 근거가 충분하다.

##### MemoryObj 소유권 규약 검증: 위반은 실재했으나 원인은 아니었다

규약은 serializer 를 backend 옆에 놓고 읽어야 보인다. `NaiveSerializer.serialize()` 는
**같은 객체**에 `ref_count_up()` 한 번을 더해 돌려주고(= 소비자 몫),
`remote_backend.batched_submit_put_task()` 는 자기 몫만 내린다:

```python
for mo in memory_objs: mo.ref_count_up()                    # backend 몫 +1
try:     compressed = [serialize(mo) for mo in memory_objs]  # 소비자 몫 +1
finally: for mo in memory_objs: mo.ref_count_down()          # 자기 몫만 -1
... connection.batched_put(keys, compressed_memory_objs)
```

`remote_backend.py` 전체에 `ref_count_down` 은 **두 곳뿐**이고 둘 다 위의 "자기 몫" 이다 —
콜백(`batched_put_callback`/`put_callback`)은 참조를 내리지 않는다. 따라서 `put()` /
`batched_put()` 에 도착하는 객체는 **커넥터가 반납해야 하는 참조**를 지닌다. 우리 커넥터는
두 경로 모두에서 반납하지 않았고, `support_batched_put()` 이 True 이므로 실제 사용 경로인
`batched_put` 이 chunk 마다 참조를 누출했다. `cache_engine.py` 의 주석
"we implicitly rely on batched_put to call ref_count_down" 도 같은 규약을 말한다.

**규약 위반은 실재했다. 그러나 고쳐도 손상은 사라지지 않았다:**

| 구성 | 실패율 | 95% CI |
|---|---|---|
| 기준 (참조 누출 상태) | 85% | [64.0, 94.8] |
| **참조 반납 추가** | **70%** | **[48.1, 85.5]** |

구간이 겹치므로 **유의한 개선이 아니다.** 수정은 유지한다 — 누출은 실재하고 풀이 영구히
회수되지 않으므로 규약상 필요하다 — 다만 **이것이 손상을 고친다고 주장하지 않는다.**

부수 관찰 하나가 중요하다: 참조를 반납하자 `Double free` 경고가 시행당 57 → 95 로
**늘었다**(총 1900건). 우리 반납이 정당하다면 줄어야 한다. 늘어난 것은 그 경고가
"refcount 가 음수가 된 사건" 을 세기 때문이고, **이미 다른 곳에서 과다 해제가 일어나
카운트가 0 인 상태**에 우리의 정당한 해제가 얹히면 또 한 건이 찍힌다. 즉 **경고 수는 우리
쪽 변경의 정당성 지표가 아니며**, 과다 해제의 주체는 LMCache 쪽이다.

##### 종합 판정

| 계층 | 상태 |
|---|---|
| raw DFS I/O (16 스레드, 반복) | **정확** (80/80) |
| LMCache `LocalCPUBackend` | **정확·결정적** |
| **LMCache DAOS remote 경로** | **70~85% 손상** |

세 줄을 나란히 놓으면 결론이 하나로 좁혀진다 — 바이트도, 전송도, 스토리지도 정상이고
LMCache 의 공통 경로도 정상인데 **remote 백엔드 경로만 깨진다.** 그리고 그 경로에서만
LMCache 자신이 MemoryObj 이중 해제를 수백~수천 건 보고한다.

**현재 상태로 이 DAOS 백엔드는 사용할 수 없다.** 이 문서의 성능 수치들은 바이트 양은
옮긴 상태에서 측정된 것이므로 대역폭 지표로는 참고가 되지만 정상 동작하는 구현의 값은
아니다.

##### 권고: 상류로 올린다

여기서 커넥터를 더 고치는 것은 근거가 없다. 남은 후보가 LMCache 내부의 MemoryObj 수명
관리이므로, **최소 재현기**를 만들어 상류에 올리는 것이 생산적이다. 재현기에 필요한
요소는 이미 다 있다:
- 손상 판정: `../tests/kv_correctness_gate.sh` (산문 프롬프트, 캐시 히트 확인 포함)
- 실패율: `../tests/kv_failure_rate.sh` (실행별 nonce, A-miss 단정, 비침습 관측)
- 계층 배제: `../tests/test_rawio_integrity.py` (DFS 는 무죄)
- 대조: `local_cpu: true` 단독 구성은 통과

##### 좋은 소식: 이제 A/B 가 싸다

기준율이 85% 이고 계측이 검증됐으므로, 진짜 수정은 20 시행으로도 명확히 드러난다
(85% → 5% 같은 변화는 즉시 유의미하다). 앞으로는 가설을 이 도구로 검증한 뒤에만 인과를
말한다.

#### (이하 무효) 원인 2: store alias 가 주원인이다 (실패율로 확정)

이 항목에서 나는 두 번 틀렸다. 처음에 소수 시행으로 "store alias 가 원인" 이라 했고,
같은 빌드가 2/6 을 내자 그것을 **철회**했다. **그 철회가 과잉이었다.** 간헐적 실패를
소수 시행으로 판단한 것이 양쪽 실수의 원인이므로, 먼저 **기준 실패율**을 쟀다
(`../tests/kv_failure_rate.sh`, 시행당 store 1회 + retrieve 3회, 20 시행):

| store 경로 | 실패 | 실패율 | 95% CI (Wilson) |
|---|---|---|---|
| **alias** (`DAOS_UNSAFE_ALIAS_STORE=1`) | 20/20 | **100%** | [83.9%, 100%] |
| **복사** (기본값) | 2/20 | **10%** | [2.8%, 30.1%] |

100% 대 10% 이므로 **alias 는 확정적인 주원인이다.** 그리고 이 수치로 앞선 관측들을
역산하면 철회가 왜 틀렸는지도 보인다 — 기준율 10% 에서 "alias 6전패" 가 나올 확률은
10⁻⁶ 로, alias 구성이 10% 와 양립하지 않는다는 뜻이었다. 반면 "복사 6/6 통과" 는
p=0.53 으로 정상이다. 설명되지 않는 것은 복사 구성의 **2/6** 한 번뿐인데(10% 기준
p=0.0012), 실행 간 변동 요인이 하나 더 있다는 신호로 남긴다.

**잔여 실패 10% 는 미해결이다.** alias 를 없애도 CI 상한이 30% 이므로 "거의 고쳐졌다" 고
말할 수 없다.

##### 분류의 한계 (설계 결함)

시행을 세 갈래로 분류하게 만들었다 — 3회 retrieve 가 모두 계산과 일치(pass), 서로는
일치하나 계산과 다름(store-side), 서로 다름(read-side). 그런데 **alias 구성의 실패
20건이 전부 read-side 로 분류됐다.** store 가 고정된 오답을 썼다면 3회가 서로 일치해야
하므로 앞뒤가 맞지 않는다.

원인은 분류가 깔고 있는 가정이다 — "저장된 객체는 불변" 은 **store 가 끝난 뒤에만**
참이다. `batched_put()` 은 비동기 제출이고 `put_time` 0.12 ms 는 큐 입력 비용일 뿐이므로,
store 완료를 기다리지 않은 이 시행에서는 retrieve 가 아직 쓰이는 중인 객체를 읽을 수
있다. 즉 **store/read 구분은 store 완료를 확인한 뒤에만 유효하다.** 그 확인을 넣지
않았으므로 이 분류로 "read 측" 이라고 단정할 수 없다 — 앞 판에서 그렇게 단정한 것도
함께 철회한다.

##### 이중 해제 경고는 판별력이 없다

시행별 `Double free occurred somewhere` 증가분이 복사 구성에서 **모든 시행 정확히 57**
이었다 — 통과한 18건 포함. alias 구성에서는 60~76. 즉 이 경고는 상시 배경이고 실패를
예측하지 못한다. 경쟁이 항상 존재하고 드물게 지는 형태와도 모순되지 않지만, **적어도
판별 지표로는 쓸 수 없다.** `LocalCPUBackend` 에서 0건이므로 DAOS remote 경로 고유라는
사실만 유효하다.

##### main 계열 커넥터도 손상된다 — 버그는 08-26 재작업보다 앞선다

지금까지 손상은 브랜치의 zero-copy alias 재작업(08-26)에서 들어온 것으로 의심됐다.
그것을 가리려고 **main 기반 브랜치**(`kv-put-refcount-and-gates` = main + put 참조 반납 +
게이트)를 **client-5** 에 올려 같은 하니스로 쟀다. client-5 는 이 목적으로 새로
프로비저닝했다(CDI 생성, 이미지 22 GB 이관, 모델 이관, 이 호스트에서 빌드한 일치
클라이언트 번들, 별도 컨테이너 `gdspool/kvlmc5`).

커넥터가 main 형태임을 확인했다: `_pack`(복사)=1, `from_buffer(view)`(alias)=0,
`_drop_put_ref`=4.

| | client-6 (브랜치, alias) | **client-5 (main, 복사)** |
|---|---|---|
| 실패율 | 85% [64.0, 94.8] | **40% [19.8, 64.3]** |
| store-side | 0 | **0** |
| read-side | 17 | **6** |
| inconclusive | 0 | **5** (히트 3회 기대에 0/1/2회) |

**main 계열도 손상된다.** 따라서 08-26 재작업은 원인이 아니거나 유일한 원인이 아니고,
**결함은 main 에 07-31 부터 있는 코드에 존재한다.** 이것은 앞 절의 "원인 C 는 GDS 미접촉
코드에 있으므로 아마 이전부터" 라는 추정을 **측정으로 뒷받침한다.**

그리고 이 결과가 하나를 확실히 기각한다. client-5 의 실패 출력은 깨진 문자열이 아니라
주제에 맞는 문장이어서 "근접 동률에서 argmax 가 뒤집힌 수치 차이" 로 의심할 여지가
있었다. 그러나 **3회 재조회가 서로 다르다**(read-side 6, store-side 0). 저장된 객체는
불변이므로 같은 키를 세 번 읽어 결과가 달라지는 것은 수치 차이로 설명되지 않는다 —
**실제 데이터가 매번 다르다.**

**단정하지 않는 것:** 40% 대 85% 비교는 교란되어 있다. 호스트가 다르고(client-5 는 SNC
켜짐 4 NUMA, client-6 은 꺼짐 2 NUMA), DAOS 컨테이너도 다르고, 코드도 다르다. 구간이
64 에서 겨우 접하므로 "main 이 더 낫다" 고 말할 근거는 없다. **같은 호스트에서 코드만
바꿔** 재야 한다.

**부수 신호:** client-5 에서 20 시행 중 5건이 히트 수 부족으로 무효였다(0/1/2회). 캐시
조회 자체가 불안정하다는 뜻이고 별도 조사 대상이다. client-6 에서는 0건이었다.

##### 재현 대상 문서에 주는 영향

`DAOS KV-cache over RoCE v4 — TTFT 17.7×` (Hub `0bf8fe7d-…`, 08-25 측정)의 수치는 **이
손상이 있는 코드로 측정된 것이다.** 08-25 시점 배포 코드는 main 계열(tip `90d8850`,
07-31)이고, 그 코드가 방금 40% 실패를 보였다. 따라서:

- `397e2c4`(GDS 직전)도, `main` 도 **"안정 버전" 이 아니다**
- 그 문서의 성능·안정성 수치는 **이 버그를 해결한 뒤 재측정**해야 한다

##### 같은 호스트 대조: 코드 차이는 원인이 아니다. 호스트가 지배 변수다

client-5 에서 호스트·하니스·컨테이너 속성·env·모델을 모두 고정하고 **코드 경로 하나와
env 플래그 하나만** 바꿔 20 시행씩 쟀다. 하니스는 한 경로에서 공유해 arm 별 사본 차이를
배제했고, 각 arm 에서 실제 로드된 커넥터 형태와 env 적용을 확인했다.

| arm | 코드 | 실패율 | 95% CI | pass/store/read |
|---|---|---|---|---|
| A: main | `_pack` 복사 | **측정 실패** (서버 미기동) | — | — |
| B: 브랜치, 복사 | `_prep_write`+copy | **40.0%** | [21.9, 61.3] | 12 / 2 / 6 |
| C: 브랜치, alias | `DAOS_UNSAFE_ALIAS_STORE=1` | **30.0%** | [14.5, 51.9] | 14 / 2 / 4 |

**alias 가설은 여기서 닫는다.** B 40% 대 C 30% 는 구간이 크게 겹치고 점추정은 alias 쪽이
오히려 낮다. 그리고 앞서 client-6 에서 얻은 "alias 100% vs copy 10%" 는 **둘 다 nonce
수정 이전**의 측정이다 — 기준 패스가 캐시 히트가 되던 깨진 하니스 값이다. 고친 하니스로
alias 를 잰 것은 이 배치가 처음이고 **효과가 없다.** 이 하위 질문에서 주장·철회를 네 번
반복했으므로 여기서 종결한다: **store alias 는 원인이 아니다.**

호스트 간 비교:

| 호스트 | 코드 | 실패율 |
|---|---|---|
| client-6 | 브랜치(복사) | 85% [64.0, 94.8] |
| client-5 | main(복사) | 40% [19.8, 64.3] |
| client-5 | 브랜치(복사) | 40% [21.9, 61.3] |
| client-5 | 브랜치(alias) | 30% [14.5, 51.9] |

**client-5 안에서 코드를 바꿔도 30~40% 로 구분되지 않고, 호스트를 바꾸면 40% → 85% 로
벌어진다.** 따라서 이 저장소의 어떤 커밋도 원인이 아니며, **"안정 버전" 을 git 이력에서
찾는 접근은 종료한다.** 두 호스트 모두 손상되므로 버그는 어디에나 있고 비율만 다르다.
client-5 는 SNC 4 NUMA, client-6 은 2 NUMA 라는 차이가 있으나 그것이 원인인지는 확인하지
않았다.

⚠️ **arm A 는 이 배치에 측정이 없다.** 로그상 서버는 요청을 처리하고 있었는데 준비 확인
루프가 실패로 판정했다 — 직전 컨테이너 `rm -f` 직후 GPU 메모리 해제와 겹친 경합으로
보인다(이 세션에서 같은 유형을 한 번 겪었다). main 의 유일한 측정값은 직전 단독 실행의
40% [19.8, 64.3] 이고 B 와 같은 값이지만, 같은 배치 안의 값은 아니다.

##### 최소 재현기: vLLM·GPU 없이 재현되고, 동시성이 방아쇠다

`../tests/repro_kv_corruption.py` 는 커넥터 + LMCache MemoryObj 할당기 + DAOS 만
구동한다. vLLM·GPU·모델·생성비교 없이 **재현된다.**

같은 호스트·같은 코드·같은 객체 크기(28 MiB)에서 **팬아웃만** 바꿔 검사 수를 맞춰 쟀다:

| arm | 조건 | 실패 | 실패율 | 95% CI |
|---|---|---|---|---|
| S | 키 1개/라운드, **팬아웃 없음** | **0/200** | **0.0%** | [0.0, 1.9] |
| C | 키 16개/라운드, **동시 `batched_get`** | **6/208** | **2.9%** | [1.3, 6.2] |

6건이 한쪽에만 몰릴 확률이 균등 가정에서 약 `0.51⁶ ≈ 1.8%` 이므로 **동시성이 방아쇠**다.

**이것이 v4 문서의 "무결성 30/30" 을 정량적으로 설명한다.** 그 시험은 28MB×30 **순차**
read 였고, 여기서 순차 실패율은 측정 0%(상한 1.9%)다. 즉 순차 시험은 n 이 얼마든 통과한다.
그 수치는 틀린 것이 아니라 **검정력이 없었다** — 객체당 1% 결함이 있어도 순차 30회가 전부
통과할 확률이 `0.99³⁰ ≈ 74%` 다. 문서 댓글에 이 내용을 달았다.

##### 오프셋이 계층을 지목한다

```
r7 k0: 3회 전부 틀림;      first diff 4194268,  1024/7168 pages, head=key 0 패턴
r7 k3: [0,2] 틀림 [1] 정확; first diff 12582876, 1024/7168,      head=key 3 패턴
r8 k0: 3회 전부 틀림;      first diff 4194268,  2048/7168 pages
r8 k2: [1,2] 틀림 [0] 정확; first diff 12582876, 1024/7168
```

헤더가 36 B(prefix 8 + meta 28)이므로 페이로드 위치 `p` 는 파일 오프셋 `p+36` 이다:

| 페이로드 위치 | 파일 오프셋 |
|---|---|
| 4194268 | **정확히 4 MiB** |
| 12582876 | **정확히 12 MiB** |

**손상이 DFS chunk 경계에서 시작하고 정확히 1024 페이지(4 MiB) 또는 2048(8 MiB) 만큼**
이다. 그리고 버퍼 앞부분은 **올바른 키의 패턴**이다 — 키 혼동이 아니라 **객체 중간의
chunk 하나가 유실**된다. `read_obj_into` 는 `payload_len` 을 반환하므로 아무 신호가 없다.

일부 객체는 3회 읽기가 **전부** 틀리고 일부는 1회만 정확하다. 안정적으로 틀린 객체가
아니므로 read 경로에 남는다.

이 서명은 이 조사 최초의 raw DFS 발견과 **동일**하다. 차이는 목적지 버퍼다 — raw 테스트는
같은 28 MiB·같은 스레드 수를 평범한 `bytearray` 로 받아 80/80 통과하고, 재현기의 MemoryObj
목적지는 실패한다. **동시 다중 chunk DFS 읽기**가 용의자이고, 목적지 버퍼 타입이 결함의
발현 여부를 가르는 변수다.

##### 목적지 버퍼는 무죄다 — 원인은 동시 다중 chunk DFS 읽기

`DAOS_READ_VIA_BYTEARRAY=1` 로 read 목적지를 전용 `bytearray` 로 바꿔 같은 동시 조건에서
같은 규모로 쟀다. **가설은 반대 방향으로 기각됐다:**

| arm | 실패 | 실패율 |
|---|---|---|
| alias-dest (MemoryObj 직접) | 4/208 | 1.9% |
| **bytearray-dest (복사)** | **13/208** | **6.2%** |

복사가 **더 나쁘다.** 즉 MemoryObj/torch 메모리 alias 는 원인이 아니고, 평범한
`bytearray` 로도 손상된다. 커넥터의 zero-copy read 설계는 무죄다.

**따라서 결함은 커넥터 아래, 동시 다중 chunk DFS 읽기 경로에 있다.**

오프셋이 이를 확정한다(헤더 36 B 보정 후):

| 페이로드 위치 | 파일 오프셋 |
|---|---|
| 4194268 | 4 MiB |
| 8388572 | 8 MiB |
| 12582876 | 12 MiB |
| 16777180 | 16 MiB |
| 20971484 | 20 MiB |

매번 정확히 1024 페이지(4 MiB) 한 덩어리이고 버퍼 앞부분은 대개 올바른 키의 데이터다 —
**객체 중간의 chunk 하나가 유실**된다. 그리고 결정적으로 한 건은
`r4 k7 ... head looks like key 14's pattern` 이었다 — **동시에 읽던 다른 키의 데이터가
섞여 들어왔다.**

**이것은 v4 문서가 libfabric RxM 으로 특정하고 UCX 전환으로 해결했다고 기록한 그 버그와
같은 계열이다**("경계 바이트가 직전 blob 데이터", bulk 버퍼 재사용). 같은 계열이 **UCX
위에도 있고** 비율만 낮다 — 당시 30개 중 20~27개 손상, 지금 객체당 2~6%.

##### ★ 순수 DFS 에서 재현된다 — LMCache 도 Python 객체 목적지도 필요 없다

`../tests/test_rawio_integrity.py` 에 burst 모드를 넣고(쓰기 후 배리어 → 모든 읽기 동시
발사) client-5 에서 같은 규모로 쟀다. **컨테이너에서 `torch` 도 `lmcache` 도 임포트하지
않고, 목적지는 평범한 `bytearray` 다.**

| mode | 실패 / 208 | 실패율 |
|---|---|---|
| loop (스레드별 write→read 반복) | 2 | 1.0% |
| burst (읽기 동시 발사) | 4 | 1.9% |

**두 모드 모두 실패한다.** 오프셋은 전부 정확한 4 MiB 배수다(헤더 36 B 보정):
8388572→8 MiB, 16777180→16 MiB, 20971484→20 MiB, 25165788→24 MiB. 매번 정확히 1024
페이지(4 MiB) 한 덩어리이고 버퍼 앞부분은 올바른 스레드의 패턴이다.

**이것이 최소 재현기다. 결함은 DAOS 클라이언트의 동시 다중 chunk 읽기에 있다** —
커넥터도, LMCache 도, MemoryObj alias 도 아니다. 상류는 LMCache 가 아니라 DAOS 다.

⚠️ **직전 판의 설명을 정정한다.** "loop 모드는 동시 읽기 밀도가 낮아 80/80 이 통과했다" 고
적었는데, **loop 모드도 여기서 실패한다.** 그 설명은 지지되지 않는다.

그러면 client-6 에서 raw 테스트가 80/80 을 반복 통과한 것은 무엇이었나 — 확실하게 말할 수
없다. 두 가지가 남는다: (a) 표본 부족 — 1% 비율에서 80회가 전부 통과할 확률은
`0.99⁸⁰ ≈ 45%` 이므로 한두 번의 통과는 우연으로 충분하다, (b) 호스트 차이 — 그 측정은
client-6, 이 측정은 client-5 다. **둘을 가르지 않았으므로 어느 쪽이라고 주장하지 않는다.**
어느 쪽이든 교훈은 같다: **80/80 도, 문서의 30/30 도 1~2% 결함을 배제하지 못한다.**

##### `dfs_sys` 핸들 캐시도 기각, 그리고 "4 MiB chunk 하나" 특성화를 철회한다

동시 읽기에서 **객체 전체가 다른 스레드의 데이터**로 오는 경우를 관측해
(`7168/7168 pages differ, head looks like thread 10's pattern`) `dfs_sys` 의 경로→핸들
캐시를 의심했다. 신선한 컨테이너·같은 chunk·같은 스레드 수에서 **`sflags` 만** 바꿔 쟀다:

| arm | 실패 / 208 | 실패율 |
|---|---|---|
| A: cache ON (운영 기본값 `sflags=0`) | 9 | 4.3% |
| B: `DFS_SYS_NO_CACHE` (락은 유지) | 4 | 1.9% |

구간이 겹치고 캐시를 끈 쪽이 오히려 낮다. **핸들 캐시는 원인이 아니다.**

⚠️ **그리고 이번 13건이 앞선 특성화를 무너뜨린다.** 오프셋(헤더 36 B 보정)과 손상 크기:

| 파일 오프셋 | chunk 경계? | 손상 크기 |
|---|---|---|
| 4, 8, 12, 24 MiB | 예 | 256~2048 페이지 |
| **3, 6, 7, 11 MiB** | **아니오** | 256~512 페이지 |

**오프셋이 항상 4 MiB 경계가 아니고 손상 크기도 1~8 MiB 로 변한다.** 앞선 판에서 소수
표본의 깔끔한 4 MiB 경계만 보고 "정확히 chunk 하나가 유실된다" 고 쓴 것은 과잉
일반화였다. 철회한다. 이 조사에서 표본 부족으로 특성화를 잘못한 것이 이번이 여러 번째다.

##### 현재 확정된 것과 열린 것

| 항목 | 상태 |
|---|---|
| 손상 실재 | **확정** — 28 MiB·16스레드에서 2~4% |
| 순수 DFS 재현 | **확정** — LMCache·torch·MemoryObj 없이, `bytearray` 목적지 |
| 위치 | **DAOS 클라이언트 동시 읽기 경로** |
| 커넥터 zero-copy / MemoryObj alias | **무죄** |
| LMCache | **무죄** |
| `daos-0002` (TSE_TASK_ARG_LEN) | **배제** — 컴파일 타임 `D_CASSERT` 가 검증, 변경은 여유를 늘리는 방향 |
| `dfs_sys` 핸들 캐시 | **기각** |
| 손상 형태 | 버퍼 앞부분은 대개 정확, 중간이 가변 크기로 깨짐. 드물게 객체 전체가 타 스레드 데이터 |
| **`daos-0004` (rkey 스텁 → HMEM 폴백)** | **열림** |

##### ★ 결론: 스톡 DAOS 2.9.100 이 손상시킨다 — 우리 패치는 무죄

버전을 고정한 스톡 대조군을 만들어 확정했다. `daos-stack/daos` 의 같은 저장소에서
**GPU-direct API 도입 커밋(`133e6f8ca`)의 부모인 `841487de8`** 를 체크아웃해
(`v2.9.100-tb` 태그가 조상) 별도 prefix 로 빌드했고, **prereq(mercury/UCX)는 패치된 것을
그대로 복사해 재사용**했다. 즉 두 arm 의 차이는 **DAOS 코어 라이브러리 하나뿐**이다.

| | A: 패치 | B: 스톡 |
|---|---|---|
| `libdaos.so` 크기 | 8950616 | **8922384** |
| `libcart` → `HG_Bulk_import_rkey` | 1 (참조) | **0** |
| DAOS 버전 | 2.9.100 | **2.9.100 동일** |
| 기준 커밋 | `c87080a70` (cuFile draft) | **`841487de8`** |
| mercury/UCX prereq | 동일 바이너리 | **동일 바이너리** |
| 서버·호스트·컨테이너 속성·테스트 | 동일 | 동일 |
| **실패 / 208** | **2 (1.0%)** | **4 (1.9%)** |

각 arm 에서 실제 로드된 `libdaos` 크기를 출력해 번들 교체를 확인했다.

**둘 다 실패하며 구간이 겹친다. `daos-0002`·`daos-0004` 는 원인이 아니다.**

⚠️ 배제되지 **않은** 변수 하나: 두 arm 이 **같은 패치 mercury/UCX prereq** 를 쓴다. 따라서
"완전 스톡 스택"(스톡 DAOS + 스톡 mercury + 스톡 UCX)은 시험하지 않았다. 다만 그 패치가
이 경로에서 무해하다는 근거는 있다 — 추가한 `HG_Bulk_import_rkey` 스텁은 **스톡 DAOS 가
호출하지 않고**, TLS 로직은 `NA_UCX_EXTRA_TLS` 가 비면 upstream 과 동일한
`ucp_config_modify(config, "TLS", tls)` 한 줄로 축약된다. 이것은 코드 근거이고 측정은
아니다. 완전 스톡 prereq 는 `--build-deps=yes` 재빌드가 필요하다.

##### 최종 인과 사슬

```
스톡 DAOS 2.9.100: 동시 다중 chunk 읽기에서 데이터 손상 (28 MiB·16스레드에서 1~4%)
  → read_obj_into 가 payload_len 을 반환하므로 조용함 (got == payload_len 통과)
  → 커넥터가 손상된 KV chunk 를 정상으로 반환
  → 프롬프트당 ~19 chunk → 재조회당 ~17-30%
  → end-to-end 게이트 30-85% 실패
```

배제된 것: 커넥터 zero-copy·MemoryObj alias·LMCache·`daos-0002`·`daos-0004`·dfs_sys 캐시·
목적지 버퍼 타입·chunk 정렬·호스트 코드 버전.

**이것은 상류 DAOS 이슈다.** 재현기는 `../tests/test_rawio_integrity.py` 하나로
충분하다 — DAOS + Python 만 필요하고 LMCache·GPU·torch·모델이 불필요하다.

##### 부수 기록: CI 클러스터(192.168.35.40/41/42)는 대조군이 못 된다

접근은 되지만 `provider: ofi+verbs;ofi_rxm` 이다 — v4 문서가 "대용량 RDMA read 를 조용히
손상시킨다" 고 특정하고 UCX 로 전환해 해결한 **바로 그 provider**. 여기서 손상이
재현되어도 RxM 버그를 본 것일 뿐이고, "스톡에서도 재현 → 우리 패치 무죄" 라는 **거짓
결론**을 낼 위험이 있다. 추가로 VM(QEMU NVMe·zvol), `targets: 1`(운영 8),
rank 0·3 Excluded, `daos_server` 전부 inactive, 클라이언트/서버 빌드 불일치
(151.g4d2012d79 / 340.g31214ce07). **다른 사람이 같은 함정에 빠지지 않도록 기록한다.**

##### 빌드 트리 주의

스톡 빌드는 `/var/daosbuild/daos-stock`(사본, `841487de8`)에서 `PREFIX=/opt/daos-stock`
으로 만들었으나 **빌드 디렉터리를 원본과 공유**한다(`/var/daosbuild/build-gpu`). 설치본
`/opt/daos-gds*` 는 무손상이지만, **패치 버전을 재빌드하려면 패치를 다시 적용해야 한다.**

##### (이전 판) 스톡 검증이 막혀 있다 — 해결됨

`daos-0004` 를 가르려면 패치 없는 구성이 필요한데 세 경로 모두 막혔다:

1. **스톡 클라이언트 + 현 서버** — 버전 불일치(원인 B)와 교란된다. 구 번들이 2.9.100 서버
   상대로 실패한 전례가 있다.
2. **서버까지 스톡 재구축** — reformat 이 필요하고 그것은 **`gdspool` 파기 + SPDK wedge
   위험**이며 client-6 의 다른 사용자에게도 영향이 간다. 파괴적이므로 지시 없이 하지 않는다.
3. **별도 CI 클러스터(192.168.35.40/41/42)** — 살아 있으나 **SSH 접근 권한이 없다**
   (`hgichon`·`root` 모두 거부).

**3번이 압도적으로 싸다.** 그 클러스터가 스톡 DAOS 라면 `tests/test_rawio_integrity.py`
하나만 옮겨 돌리면 끝난다 — 재현되면 상류 DAOS 이슈, 안 되면 우리 패치가 원인.
1. **store 완료를 확인하는 시행**으로 분류를 유효화한다. 그 전에는 store/read 구분에
   근거가 없다.
2. 잔여 10% 를 좁히려면 시행 수가 필요하다 — 10% 대 2% 를 가리려면 수백 시행이다.
   큰 효과(alias 100%)는 20 시행으로 충분했지만 잔여는 그렇지 않다.
3. `_get_sync()` 의 MemoryObj 소유권 규약은 여전히 후보다. 커넥터에
   `ref_count_down`/`release`/`free` 를 순서대로 시도하며 예외를 삼키는 코드가 세 곳
   있어 LMCache 버전에 따라 이중 해제가 되기 쉽다.

### 이것이 v2 포맷 계획의 우선순위를 바꾼다

`PLAN.md` Phase 1 의 v2 포맷은 "GPU 등록·DMA 정렬" 을 위한 것이었는데, 지금은 **정합성
문제**로 승격된다. 다만 4 KiB 헤더로는 부족하다 — 4 KiB 정렬은 4 MiB chunk straddling 을
없애지 못한다. 후보:

1. **페이로드를 chunk 크기에 정렬한다** (헤더를 4 MiB 로 패딩, 또는 메타데이터를 별도
   객체/dkey 로 분리해 페이로드가 오프셋 0 에서 시작하게 한다). straddling 자체를 없앤다.
2. **비정렬 다중 chunk 동시 전송 경로의 실제 버그를 찾는다** — DFS 바인딩,
   `dfs_sys_read` 사용법, 또는 DAOS array 계층. 근본 수정이지만 범위가 크다.
3. 임시 완화: 워커를 2 이하로 제한. 정확하지만 처리량을 버린다.

~~**1번을 먼저 하고 2번을 병행 조사하는 것을 권한다.**~~ **정렬 가설이 반증되었으므로 위
세 후보는 무효다.** 원인 1은 위에서 확정했고(mercury 패치의 cuda TLS), 원인 2는 store 측
alias 경합이 유력하다. v2 포맷은 정합성 근거가 사라졌으므로 원래대로 GPU 정렬 항목으로
되돌린다 — 다만 지금은 GPU-direct 를 만들지 않기로 했으므로 우선순위가 낮다.

어떤 성능 작업보다 원인 2 가 우선이다 — 지금 상태로는 KV 캐시가 조용히 틀린 값을 준다.

## LMCache 의 fused c_ops 는 이 이미지에서 한 번도 동작한 적이 없다

배칭 착수 전 발견한 첫 번째 문제다. `import lmcache.c_ops` 가 조용히
`python_ops_fallback.py` 로 대체된다:

```
Failed to import backend lmcache.c_ops: libcudart.so.13: cannot open shared object file
```

컴파일된 `c_ops.cpython-310-x86_64-linux-gnu.so`(31.8 MB)는 이미지에 있지만 로드되지
않는다. `kvsup:052` 는 **torch 2.10.0+cu128** 위에 **CUDA 13 으로 빌드된 LMCache 휠**을
얹었다. 확인한 것:

- LMCache 0.5.0 / 0.5.1 / 0.5.2 / 0.5.3 휠 **전부** `libcudart.so.13` 을 요구한다.
  버전을 내려도 해결되지 않는다.
- c_ops 가 참조하는 c10 심볼 47개 중 **딱 2개**가 이 torch 에 없다:
  `c10::cuda::CUDAStream::query()` 와 `::synchronize()` (이 빌드에서는 헤더 인라인).

**따라서 이 문서의 모든 GPU⇄CPU 복사 수치(to_gpu 163.9 ms, from_gpu 61.0 ms,
3.84/10.3 GB/s)는 fused CUDA 커널이 아니라 Python 폴백에서 나온 값이다.**

빠진 심볼 2개를 `stream()`(이건 export 되어 있다)으로 정의하는 shim
(`../tests/c10_cudastream_shim.cpp`)을 만들면 fused 확장이 로드되고 시작 경고도 사라진다.
그러나 **정합성 게이트가 4개 중 3개 불일치로 실패했다.** 그래서 fused 커널의 값어치는
이 방법으로 측정할 수 없고, shim 은 배포에 쓸 수 없다. 근본 해결은 이미지의 torch 와
LMCache 빌드를 맞추는 것(또는 LMCache 를 cu128 torch 로 소스 빌드)이다.

shim 은 측정 도구로만 저장소에 남긴다. 파일 상단에 배포 금지 사유를 적었다.

### 측정 이력과 정정

이 문서의 앞선 판들은 잘못된 수치를 실었다. 앞의 세 번은 매번 **컨테이너 구성** 이 원인이었고,
네 번째는 **분모를 잘못 잰 것** 이었다.

| 판 | 주장 | 실제 |
|---|---|---|
| 1판 | `pinnedcopy` 대비 대역폭 2.28×, 포화점 2.44×, CPU 1.86× 개선 | chunk 1 MiB 산물. 4 MiB 에서 소멸 |
| 2판 | 대역폭 이득 없음(12.05 vs 12.05), DRAM 만 남음 | `RP_2G1` 산물. 넓은 oclass 에서 스테이징이 25.6 으로 올라가 GPU-direct 가 오히려 짐 |
| 3판 | 대역폭·지연 열세(16W 0.59배), DRAM 만 우세 | cell1 이 NVMe 7개인 비대칭 구성. 대칭 8+8 에서 스테이징이 35.6 까지 올라가 격차가 더 벌어짐 |
| 4판 | DRAM 상한 COPY 136.6 GB/s, 스테이징이 그 60% 소비, "GPU 4장이면 필연" | 단순 루프로 잰 하한. 실제 401.7 GB/s → 스테이징은 20%. 그리고 4장 외삽은 per-GPU 와 집계를 혼동한 오류 |
| 현재 | 16W 0.68배·1W 0.52배 열세, DRAM 7.8~19× 우세, 교차점 집계 138 GB/s | 위 표 |

교훈이 둘이다. **스토리지 구성 기본값을 고정하지 않은 비교는 두 경로에 비대칭으로 작용한다**
— 좁은 청크와 좁은 oclass 는 스테이징 경로를 훨씬 더 세게 때린다. 그리고 **비율을 주장할
때는 분모도 분자와 같은 엄격함으로 재야 한다** — 4판의 결론을 뒤집은 것은 새로운 분자가
아니라 제대로 잰 분모였다.

## 2026-09-05 재측정 — 분리된 드라이브 위에서도 결론은 같다

이 README 의 모든 수치는 cell1/cell2 가 같은 듀얼포트 NVMe 를 두 랭크가 함께 쓰던 오구성
상태에서 측정됐다(`DAOS-CONCURRENT-READ-CORRUPTION.md` §62). 드라이브를 분리한 뒤(랭크당 4 대,
cell1 02~05 / cell2 06~09) GDS 엔진 빌드(`/opt/daos-gds`, 양 cell) + `ucx+rc_v` +
클라이언트 `/opt/daos-gds-gpu` 로 같은 5-arm 벤치를 다시 돌렸다. 풀 `attr1`, 컨테이너 chunk 4 MiB,
rd_fac:0, 요청 32 MiB, 8 GiB, client-5(H100 NVL, SNC). `NA_UCX_EXTRA_TLS=cuda_copy,cuda_ipc`.

정합성: `dfs_gpu_rt` 4 KiB / 64 KiB / 1 MiB / 32 MiB 왕복 **ALL OK** (S16, RP_2G4 둘 다).

| 컨테이너 | 워커 | `gpu` | `pinnedcopy` | `pinned` | gpu/staging | DRAM_x gpu / pinnedcopy |
|---|---|---|---|---|---|---|
| S16 | 1 | 6.17 | 10.77 | 13.42 | 0.57 | 0.11 / 0.10 |
| S16 | 4 | 9.35 | 27.03 | 28.52 | 0.35 | 0.30 / 0.67 |
| S16 | 16 | 16.01 | **35.12** | 35.33 | **0.46** | 0.29 / 1.88 |
| RP_2G4 | 1 | 8.20 | 10.18 | 12.29 | 0.81 | 0.11 / 0.10 |
| RP_2G4 | 4 | 12.52 | 22.02 | 14.52 | 0.57 | 0.20 / 0.75 |
| RP_2G4 | 16 | 19.74 | 24.89 | 25.50 | 0.79 | 0.26 / 1.81 |

읽는 법:
- **GPU-direct 는 여전히 대역폭에서 진다.** 16 워커에서 S16 0.46배, RP_2G4 0.79배. `gpu` 는
  16~20 GB/s 에 머물고(§"GPU BAR 쓰기는 QP 당 제한된다" 의 1 QP 천장 22 와 일치), 호스트 경로는
  S16 에서 35 GB/s(랭크당 4 대로도 8 대 시절 35.6 재현)까지 간다. 그룹이 넓을수록(S16 = 16 샤드)
  전송 단위가 작아져 GPU 경로만 더 손해를 보는 것도 그대로다(RP_2G4 19.7 vs S16 16.0).
- **남는 이득도 그대로 DRAM 트래픽만이다**: 16 워커 DRAM_x 0.26~0.29 vs 1.8~1.9 (약 7×).
- **오구성은 이 결론에 영향을 주지 않았다.** 전송 계층의 병목(QP 당 BAR write)은 스토리지와 무관하다.
- **mercury 패치(`cuda_copy,cuda_ipc` TLS)가 호스트 전송을 깨뜨린다는 판정은 재검토가 필요하다.**
  TLS on/off 에서 호스트 경로 대역폭은 같고(S16 16W pinnedcopy 35.12 vs 34.88), 당시의 "4 MiB
  한 덩어리 손상" 은 공유 드라이브 오구성과 같은 시기·같은 증상이다. 여기서는 대역폭만 재봤고
  호스트 경로 정합성은 `dfs_integrity` 로 따로 확인해야 한다. 패치 기본값 opt-in 은 그대로 둔다.

**판정 유지: `DaosGdsBackend` 는 만들지 않는다**(`PLAN.md` §0 재개 조건 불변). 재측정 뒤 클러스터는
운영 권고 구성(stockfull + `ofi+verbs;ofi_rxm`)으로 되돌렸다. GDS 경로는 UCX 전용이므로 다시 시험하려면
양 cell `override.conf` 를 `/opt/daos-gds` 로, provider 를 `ucx+rc_v` 로, client-5 agent domain 을
`mlx5_0:1` 로 바꾸고 재포맷해야 한다(약 10 분; 절차는 이 세션의 `deploy/README.md` §9 전송 교체와 같다).

## 2026-09-06 GDS over `ofi+verbs;ofi_rxm` — 동작하고, 스테이징에 근접한다

UCX 경로의 16~20 GB/s 를 "GPU BAR write 의 QP 당 하드웨어 천장" 으로 읽었던 것은 **틀렸다.** 같은 GDS 빌드·같은
드라이브·같은 클라이언트에서 전송만 libfabric verbs 로 바꾸면 GPU-direct 가 35 GB/s 까지 올라간다. 병목은 UCX 경로 안에 있었다.

### 필요했던 것 세 가지 (UCX 쪽 mercury 패치는 불필요)
1. **cart 에 메모리 디바이스 지원 켜기**: `D_MEM_DEVICE=1`(또는 `D_GPU_DIRECT=1`). 초안의 cart 는
   `crt_mem_device_enabled()` 가 참일 때만 mercury 에 `request_mem_device` 를 넘기고, 그래야 na_ofi 가 도메인을 `FI_HMEM` 으로 연다.
   없으면 `NA_Mem_register() failed (NA_OPNOTSUPPORTED)`.
2. **libfabric 을 CUDA 지원으로 빌드**: 초안 빌드의 libfabric 1.25 는 `--with-cuda` 없이 빌드돼 `FI_HMEM_CUDA not supported`.
   `CPPFLAGS=-I/usr/local/cuda/include LDFLAGS="-L/usr/local/cuda/lib64 -L/usr/local/cuda/lib64/stubs" ./configure … --with-cuda=/usr/local/cuda --enable-cuda-dlopen`
   (기본 `--with-cuda=DIR` 만으로는 configure 가 `-lcudart` 를 못 찾는다). 런타임에 `libcudart.so`·`libcuda.so`(버전 없는 이름) 를 dlopen 하므로
   LD 경로에 심볼릭 링크가 필요하다(`/opt/ofi-cuda/lib64/libcudart.so -> /usr/local/cuda/lib64/libcudart.so.13`).
3. **libfabric verbs 패치** `patches/libfabric-0001-verbs-cuda-dmabuf-and-close-fd.patch`(2 헝크):
   - `vrb_mr_reg_common()` 이 dmabuf 등록 경로를 ZE/ROCR/SYNAPSEAI 에만 쓰고 **CUDA 는 `ibv_reg_mr` 로 떨어뜨린다** → peermem 없는
     플랫폼에서 `-14 (Bad address)`. 조건에 `FI_HMEM_CUDA` 추가.
   - `vrb_reg_hmem_dmabuf()` 가 `ibv_reg_dmabuf_mr()` 뒤 **dma-buf fd 를 닫지 않아 등록마다 fd 1 개 누수** → MR 캐시가 꺼진 상태에서 4 MiB
     청크 2048 개(8 GiB) 를 읽으면 nofile 1024 에 걸려 `cuMemGetHandleForAddressRange: CUDA_ERROR_OPERATING_SYSTEM`. `close(fd)` 추가
     (실측: 1.5 s 에 dmabuf fd 634 개 → 패치 후 1 개).
   둘 다 ucx-0001 과 같은 "dmabuf-only 플랫폼 회귀" 성격이라 상류 제출 가치가 있다.

### 결과 (분리 드라이브, GDS 엔진 `/opt/daos-gds`, `ofi+verbs;ofi_rxm`, client-5, chunk 4 MiB, 32 MiB 요청, 8 GiB)
정합성: `dfs_gpu_rt` 4 KiB~32 MiB 왕복 ALL OK(S16, 3 회). fd 누수 없음.

| 컨테이너 | 워커 | `gpu` | `pinnedcopy` | `pinned` | gpu/staging | (UCX 였을 때 gpu) |
|---|---|---|---|---|---|---|
| S16 | 1 | **11.63** | 10.42 | 12.80 | **1.12** | 6.17 |
| S16 | 4 | 26.67 | 27.99 | 30.78 | 0.95 | 9.35 |
| S16 | 16 | **35.26** (3 회 35.1~35.4) | 41.21 | 42.43 | **0.86** | 16.01 |
| RP_2G4 | 1 | 11.88 | 10.38 | 13.21 | 1.14 | 8.20 |
| RP_2G4 | 4 | 24.11 | 23.52 | 23.59 | 1.03 | 12.52 |
| RP_2G4 | 16 | 23.85 | 23.48 | 24.34 | **1.02** | 19.74 |

DRAM 트래픽(16W, S16, perf uncore_imc): `gpu` read 999 + write 887 MiB vs `pinnedcopy` 7557 + 7542 MiB → **약 8× 절감**, 전달 바이트당 0.23 vs 1.84.

읽는 법:
- verbs 에서는 GPU-direct 가 **1 워커에서 스테이징을 앞서고**(복사 한 단계가 없으니 당연한 방향), 16 워커에서도 0.86 배로 UCX 의 0.46 배와
  차원이 다르다. RP_2G4 처럼 스토리지 쪽이 상한(24~25 GB/s)이면 두 경로가 같다 — 즉 GPU 경로 자체의 천장은 이 구성에서 최소 35 GB/s 이상이다.
- 따라서 §4-2 의 "QP 당 22 GB/s" 는 perftest 조건(1 QP)의 사실이지만 DAOS 의 GPU 경로 상한을 설명하지 못한다. DAOS 는 16 xstream 에서 16 QP
  로 밀어넣으므로 원래 22 에 갇힐 이유가 없었고, 실제로 UCX 만 갇혔다. UCX 쪽 손실(CUDA 목적지에 대한 프로토콜 선택, rndv 조각화 등)은
  미규명이며 verbs 가 답이 된 이상 파지 않는다.
- 호스트 스테이징도 verbs 가 UCX 보다 빠르다(S16 16W 41~42 vs 35).

### 남은 문제
- **RP_2 컨테이너에 GPU 소스로 쓰기(`dfs_write_gpu`)가 verbs 에서 실패**: 64 KiB 부터 follower(rank 1 tag 9) 로의 update 가 `DER_HG` →
  15 s 뒤 `DER_CANCELED`. S16(복제 없음) 쓰기·읽기와 RP_2G4 **읽기**는 정상, UCX 에서는 RP_2G4 쓰기도 정상이었다. 서버 로그에 ERR 없음.
  복제 update 의 follower 가 클라이언트 GPU 메모리를 bulk GET 하는 경로로 보이며 미규명. KV 캐시 용도는 rd_fac:0/S16 이라 영향 없음.
- MR 캐시가 `uffd`/`memhooks` 모니터 초기화 실패("No space left on device")로 꺼져 있어 I/O 마다 등록한다. 등록 비용은 위 수치에 포함돼 있다.
- 서버 측은 GDS 엔진 빌드(`/opt/daos-gds`) 가 필요했는가는 미확인 — 클라이언트 측 변경만이므로 stockfull 서버로도 될 가능성이 크다(다음 시험).

### 판단에 미치는 영향
이 문서 앞부분과 8/31 정정의 "GDS 는 실제 청크 크기에서 2 배 느리다 → `DaosGdsBackend` 를 만들지 않는다" 는 **UCX 경로에서만 성립하는
결론**이었다. verbs(운영 권고 전송)에서는 대역폭 손실이 0.86~1.1 배로 사라지고 DRAM 절감 8 배만 남으므로, `PLAN.md` §0 의 재개 조건
("GDS 가 실제 청크 크기에서 스테이징보다 느리지 않다")이 **충족됐다.** LMCache 수준 GDS 백엔드는 다시 검토 대상이다. 다음 확인 순서:
(1) stockfull 서버 + GDS 클라이언트 조합, (2) 실제 KV 청크(28~40 MiB) 크기의 TTFT 비교, (3) 8-GPU 호스트에서의 DRAM 경합.

## 2026-09-06 Phase 1 — stockfull 서버 + GDS 클라이언트 (서버 변경 불필요 확인)

서버를 운영 구성(`/var/daos-stockfull` 2.9.100, `ofi+verbs;ofi_rxm`, 드라이브 분리) 그대로 두고 client-5 에서 GDS 클라이언트
(`/opt/daos-gds-gpu` + `/opt/ofi-cuda` + `D_MEM_DEVICE=1`)만 썼다. 같은 풀에서 stockfull 클라이언트의 MP 스택(`kvlmc5`)이 동시에 돌고 있었다.

- `dfs_gpu_rt` 4 KiB~32 MiB 왕복 **3/3 ALL OK** — GDS 초안 클라이언트와 stock 서버는 wire 호환이다(초안의 변경은 클라이언트 측 메모리
  속성·등록 경로에 국한). `PLAN.md` §1 의 "서버 설정 변경을 요구하는 설계 금지" 제약을 GDS 가 만족한다.
- MP 스택은 영향 없음(8001 응답 정상).

| 워커 | `gpu` | `pinnedcopy` | `pinned` | gpu/staging |
|---|---|---|---|---|
| 1 | 11.50 | 10.22 | 12.43 | 1.13 |
| 4 | 25.08 | 28.10 | 29.75 | 0.89 |
| 16 | 35.33 | 41.99 | 42.09 | 0.84 |
| 16, **40 MiB 요청**(Qwen3-14B 의 LMCache 청크 크기) | 35.10 | 40.22 | — | 0.87 |

DRAM(16W): gpu 971+895 MiB vs pinnedcopy 7428+7357 MiB (8.0×). GDS 엔진 빌드로 잰 앞 절과 같은 값이다.

다음 단계(Phase 2, 계획서 §2~§4 의 Phase 1~3 에 해당): v2 정렬 포맷 → `dfs_read_gpu`/`dfs_write_gpu` 바인딩(GPU slab 1 회 등록·재사용)
→ in-process `DaosGdsBackend`(GPU 스테이징 + 페이지드 KV 로 D2D scatter) → 실제 KV 청크 TTFT 를 스테이징 경로와 비교.
전제 조건은 컨테이너 이미지에 GDS 클라이언트 번들(`/opt/daos-gds-gpu` lib64 + `/opt/ofi-cuda` + CUDA 링크)을 넣는 것.

## 2026-09-06 Phase 2 — in-process `DaosGdsBackend` end-to-end (vLLM + LMCache 0.5.2)

계획서 §2~§4 의 세 단계를 구현해 client-5 에서 실제 KV 캐시로 검증했다(서버 stockfull + `ofi+verbs`, 컨테이너 `kvgds_s16`).
- `lmcache_daos/serde_v2.py`: 4 KiB 헤더 페이지 + payload @4096, committed 플래그 + CRC, temp→`dfs_move` 원자 게시, `/v2` 네임스페이스. 단위 테스트 8/8.
- `dfs_binding.py`: `dfs_read_gpu`/`dfs_write_gpu`(`daos_mem_attr_t`), `dfs_lookup`/`dfs_release`/`dfs_move`, `LMCACHE_DAOS_LIBDIR` 번들 선택. C shim 대신 ctypes
  (계획서 §3 은 shim 을 권했지만 caller-owned sgl 로 충분했다). `tests/test_gds_binding.py`: torch GPU 버퍼 왕복 40 MiB 3/3, 4 MiB, 256 MiB 바이트 일치.
- `lmcache_daos/gds_backend.py::DaosGdsBackend(AllocatorBackendInterface)`: `storage_plugins: ["daosgds"]` 로 로드(T-check 통과 — 0.5.2 의
  `storage_plugin_launcher` 가 out-of-tree `module_path` 를 받는다). 자체 `GPUMemoryAllocator` 풀을 가지므로 스토리지 매니저가 store 객체를 GPU 로 복사해
  넘기고(`allocate_and_copy_objects`), retrieve 는 GPU 객체를 할당해 `dfs_read_gpu` 로 채운 뒤 GPU 커넥터가 D2D scatter 한다. 런처 `deploy/launchers/run_vllm_gds_c5.sh`.

구현 중 배운 계약 세 가지(각각 한 번씩 엔진을 죽였다):
1. `batched_contains()` 는 bool 리스트가 아니라 **접두 히트 개수(int)** 를 돌려야 한다(매니저가 `keys[:n]` 으로 슬라이스). 틀리면 lookup 이 3 s 타임아웃을 반복해 TTFT 가 3.8 s 가 된다.
2. `RemoteMetadata.serialize()` 는 RemoteBackend 가 프로세스 전역 포맷을 초기화해야 동작한다 → 플러그인은 자체 메타데이터(JSON, 헤더 페이지 안)를 쓴다.
3. libfabric 의 CUDA 등록(`cuMemGetAddressRange`, dma-buf export)은 **호출 스레드에 CUDA 컨텍스트**가 있어야 한다. 스레드풀 워커는 없으므로
   `CUDA_ERROR_INVALID_CONTEXT` → `DER_HG_FATAL` → 엔진 사망. 워커마다 한 번 `torch.cuda.set_device()` 로 컨텍스트를 올린다.

### Part A — 재시작 후 콜드 hit(모든 청크 DAOS→GPU), Qwen3-14B, ms
| ctx | GDS in-process 1번째 / 2번째 | MP verbs 콜드 | MP L1 warm | in-process 스테이징 콜드 | Hub in-process |
|---|---|---|---|---|---|
| 8K | **149 / 76** | 139 | 105 | 151~221 | 151 |
| 16K | **119 / 118** | 158 | 85 | 251~444 | 298 |
| 31K | **211 / 198** | 281 | 141 | — | 437 |

retrieve 로그: 16K 2.5 GB 91 ms(백엔드 88 ms, 30.4 GB/s), 31K 4.84 GB 163 ms(157 ms, 33.2 GB/s). 31K hit 한 건 동안 호스트 DRAM 트래픽 read 893 + write 847 MiB(스테이징은 ~5 GB × 2).
**콜드 hit 가 처음으로 L1 warm hit 수준에 왔다** — DAOS→L1→GPU 두 단계가 DAOS→GPU 한 단계가 됐기 때문이다. 게이트 3/3 PASS, put/get 오류 0.

### Part B — 100 GB working set, 12 inflight, 149 쿼리
| arm | avg | p50 | p95 | 집계 | populate |
|---|---|---|---|---|---|
| GDS in-process, 재시작 후 콜드 | 363 | 359 | 477 | 21.7 GB/s | 45 s |
| GDS in-process, inflight 6 | 195 | 196 | **201** | 20.4 | — |
| MP verbs 콜드(비교) | 216 | 210 | 295 | 36.2 | 42 s |
| Hub in-process 스테이징 | 371 | 356 | 547 | 21.4 | 63 s |

집계는 MP 에 진다. 스토리지가 아니라 **in-process 엔진이 요청별 retrieve 를 직렬로 실행**하기 때문이다(백엔드 로그의 640 MiB 읽기가 23~26 ms 씩
순차로 찍힘 = 26~29 GB/s 단건, 겹침 없음; Hub §7-4a 가 지적한 구조 그대로). 대신 12 inflight 에서 p95 가 안정적이고(477, 큐잉만), inflight 6 에서는
p95 201 ms 로 MP 의 151 에 근접한다. **DRAM: 100 GB 를 GPU 로 가져오는 동안 호스트 DRAM 트래픽이 read 4.5 + write 4.3 GB(0.09 B/B)** — 스테이징의 1.8~1.9 B/B
대비 20 배 이상 절감으로, 계획서 §6 의 합격 기준(≤ 0.3 B/B)을 크게 넘는다.

### async prefetch — 요청 간 겹침 (2026-09-06)
`enable_async_loading: True` 면 스토리지 매니저가 **lookup 시점**(스케줄 전)에 백엔드의 `batched_async_contains` → `batched_get_non_blocking` 을 이벤트
루프에서 돌려 결과를 이벤트 매니저로 retrieve 에 넘긴다(LocalCPUBackend 를 거치지 않음). 두 메서드를 구현해(스레드풀 read 를 `run_in_executor`
로 gather, 접두 절단) 다음 요청의 DAOS→GPU 읽기가 현재 요청의 prefill 과 겹치게 했다. 계획서 §1 의 "GDS+async = hang" 은 in-tree GdsBackend 에
이 메서드가 없어서 난 것이다.
그런데 0.5.2 는 prefetch 직렬화기를 `AsyncSingleSerializer`(asyncio.Lock, 한 번에 한 요청)로 **하드코딩**하고, 청크 예산 가중 세마포어인
`AsyncMultiSerializer` 는 같은 파일에 있으나 선택 경로가 없다. 플러그인 import 가 `StorageManager.__init__` 안(직렬화기 생성 전)에서 일어나므로
그 이름을 우리 GPU 풀의 청크 예산으로 만든 multi 로 되묶는다(`DAOS_GDS_MULTI_PREFETCH=1`, 기본 on; 런처 `MULTI=`).

| Part B 100 GB, 149 쿼리 | avg | p50 | p95 | 집계 |
|---|---|---|---|---|
| sync (재시작 후 콜드) | 363 | 359 | 477 | 21.7 GB/s |
| async + single serializer | 303~311 | 319~328 | 349~356 | 25.5~26.1 |
| **async + multi serializer**, 12 inflight | **285~302** | 290~299 | **320~345** | **26.5~27.7** |
| async + multi, 6 inflight | 163 | 163 | **182** | 24.4 |
| async + multi, 24 inflight (풀 10 GiB 부족) | 1512 | 1579 | 2512 | 10.6 |
| MP verbs 콜드(비교) | 216 | 210 | 295 | 36.2 |

Part A 콜드는 그대로(80 / 132~138 / 215~218 ms). 남은 격차(27.7 vs 36.2)는 엔진 쪽 — retrieve 의 `to_gpu` 청크 루프와 요청 단위 직렬 처리 — 이고 스토리지 읽기
자체는 요청당 23~25 ms(26~30 GB/s)로 MP 어댑터와 같다. **GPU 풀은 inflight × KV 크기 이상**이어야 한다: 24 inflight × 640 MB = 15 GB > 10 GiB 에서
할당 실패 → 접두 절단 → 재계산으로 급락한다(정합성은 유지). 가중 세마포어는 prefetch 동시성만 막고 소비 지연은 못 막으므로 여유를 둘 것
(`GDS_GB`, `--gpu-memory-utilization` 과 상충).

MR 캐시: mercury na_ofi 가 `FI_MR_CACHE_MAX_COUNT=0` 을 강제해 libfabric 캐시가 꺼진 상태(`ofi_mr_cache_init` → ENOSPC). 환경변수 덮어쓰기는 듣지 않고,
16 워커 34.3~34.9 GB/s 로 대역폭 영향도 없어 그대로 둔다.

### 판정
- 단일 GPU·단일 요청 콜드 지연과 DRAM 은 GDS in-process 가 최선이고, 다중 요청 집계 처리량은 MP(pinned L1 + 서버측 겹침)가 최선이다.
  8-GPU 호스트(계획서 §6)에서는 집계 = GPU 수 × 단일 GPU 이므로 엔진 직렬화가 GPU 단위로 병렬화되고 DRAM 절감이 결정적이 된다 — 미측정(장비 없음).
- 남은 것: (1) `to_gpu` 청크 루프의 Python 고정비와 요청 단위 직렬 retrieve(§"운영 경로의 병목은 전송이 아니다") — 27.7 → 36 GB/s 의 격차, (2) RP_2 GPU 소스 쓰기 결함,
  (3) libfabric 패치 2 건·multi serializer 선택 옵션의 상류 제출, (4) 8-GPU 실측.

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
- **DRAM 이득의 값어치는 단일 GPU 에서만 확인했다.** 다중 GPU 는 전부 계산이다. 남은
  미검증 항목 두 개가 교차점을 크게 움직인다: (1) 8-GPU 추론의 DRAM 소비를 1-GPU 값의
  선형 외삽으로 잡았고, (2) 스테이징 버퍼가 실제로 두 소켓에 분산되는지 재지 않았다
  — 분산이면 교차점 138 GB/s, NIC 소켓 집중이면 79 GB/s 로 1.75배 차이가 난다.
  DRAM 상한 자체는 더 이상 하한 추정이 아니다(`bench_dram_ceiling.c` 로 채널 카운터
  교차검증까지 완료).
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
