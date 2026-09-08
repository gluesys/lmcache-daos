# lmcache-daos

**[LMCache](https://github.com/LMCache/LMCache) 의 KV cache 오프로딩 백엔드를
[DAOS](https://github.com/daos-stack/daos) 로 구현한 out-of-tree 커넥터.**

LMCache 의 `RemoteConnector` 인터페이스에 붙어, vLLM 이 계산한 KV 청크를 DAOS DFS
(`dfs_sys` API) 네임스페이스의 self-describing 파일 하나로 저장한다. dfuse 나 커널
VFS 를 거치지 않고 `libdfs` 를 ctypes 로 직접 호출한다.

vLLM·LMCache 상류를 고치지 않는다 — `plugin://` 스킴과 `remote_storage_plugins`
설정만으로 로드되는 플러그인이다.

## 특징

- **상류 무수정.** vLLM·LMCache 포크나 패치 없이 설정만으로 붙는다.
- **DAOS 네이티브 경로.** `dfs_sys_*` 직접 호출. Samba `vfs_daos` 와 같은 API 계열이다.
- **객체가 자기 자신을 기술한다.** `[prefix 8B][meta][payload]` 한 파일 = KV 청크 하나.
  read 시 별도 stat·index 조회가 없고 외부 메타 DB를 요구하지 않는다. 잘린 객체는
  read 시점에 판정해 miss 로 돌린다(오탐 hit 없음).
- **노드 경계를 넘는 재사용.** 한 노드가 넣은 KV 를 다른 노드가 그대로 hit 한다.
  vLLM 프로세스를 완전히 재시작해도 유지된다.
- **용량 관리 수단.** `list()`(readdir) + `remove_sync()`. `RemoteBackend.remove()` 가
  타는 원격 eviction 경로다(정책 자체는 미구현 — [현황과 한계](#현황과-한계)).
- **batched get/put.** 커넥터 API 수준 GET 2.4–3.5x, PUT 1.3–2.4x.
  `batched_contains` 는 실측 이득이 없어 의도적으로 미구현이다.
- **비블로킹.** blocking libdfs 호출은 스레드풀 executor 로 분리해 LMCache 의 asyncio
  루프를 막지 않는다.
- **튜닝 손잡이 노출.** DFS chunk / oclass / `rd_fac` 로 read 대역폭을 직접 조절한다.

측정 수치와 그 조건, 설계 근거는 [`doc/DESIGN-AND-VALIDATION.md`](doc/DESIGN-AND-VALIDATION.md)
에 있다.

## 동작 방식

![lmcache-daos 소프트웨어 / 하드웨어 스택](doc/figures/fig1b_lmcache_daos_stack.png)

```
vLLM ─ LMCache ─ RemoteBackend ─(plugin:// 스킴)─ DaosConnector (connector.py)
                                                   └─ DfsSys ctypes 바인딩 (dfs_binding.py)
                                                        └─ libdfs dfs_sys_* → DAOS Array 객체(파일)
```

- **키 매핑**: `CacheEngineKey` → sha256 → DFS 경로 (flat)
- **객체 레이아웃**: `serde.py` — `[prefix 8B][header][payload]`
- **열거·삭제**: `dfs_sys_opendir/readdir/closedir`, `dfs_sys_remove_type`

## 요구사항

| 항목 | 버전 / 조건 |
|---|---|
| Python | 커넥터 자체는 ≥ 3.8. **실운영은 ≥ 3.10** — LMCache·vLLM 이 `requires_python >=3.10` 이고 LMCache 0.5.2 휠은 cp310–cp313 뿐이다 |
| LMCache | **v0.5.2** |
| vLLM | **≥ 0.26.0** |
| DAOS 클라이언트 | 2.8 / 2.9 (`libdaos`, `libdfs`), `daos_agent` 실행 중, **서버와 같은 버전** |
| DAOS 컨테이너 | `--type POSIX` |
| 백킹 디바이스 | **NVMe 권장** — ZFS zvol 풀에서는 DAOS 로드가 prefill 재계산보다 느려 캐시가 손해다 |

런타임 의존성 패키지는 없다. 시스템 `libdaos`/`libdfs` 를 ctypes 로 열고, LMCache 는
서빙 환경에 이미 있다고 가정한다. 런타임 박스에서 `pip show lmcache vllm` 으로 버전을
먼저 확인할 것 — 버전이 다르면 `connector.py` 의 API 배선과 아래 URL 규칙을 재확인해야
한다.

Rocky 9 처럼 시스템 `python3` 이 3.9 인 배포판에서는 커넥터만 설치·테스트(`test_serde.py`)는
되지만 LMCache 가 올라가지 않는다. `python3.11`/`python3.12` 로 venv 를 만들어 쓸 것.

### 모드별 추가 요구사항

| 모드 | 추가 빌드 | 추가 런타임 요구 |
|---|---|---|
| **in-process** (`DaosConnector`) | 없음 | stock DAOS 클라이언트 |
| **MP** (`lmcache_daos.mp`) | 없음 | 위와 같음 + 별도 MP 캐시 서버 프로세스 |
| **GDS** (`lmcache_daos.gds_backend`) | **필요** — DAOS 초안 트리 + 패치 7개, UCX·Mercury·libfabric 재빌드 | CUDA-enabled libfabric, nvidia **open** 커널 모듈, gdrcopy, replicated oclass 컨테이너 |

## 설치

### 1. DAOS 클라이언트

이 커넥터는 `libdaos`/`libdfs` 를 ctypes 로 열기 때문에 클라이언트가 먼저 있어야 한다.
없으면 `OSError: libdaos.so: cannot open shared object file` 로 즉시 실패한다.

```bash
# upstream 배포 — EL8/EL9 동일 레이아웃. 릴리스는 v2.8 까지 올라와 있다
curl -so /etc/yum.repos.d/daos.repo \
    https://packages.daos.io/v2.8/EL9/packages/x86_64/daos_packages.repo
dnf -y --allowerasing install daos-client

cp <your>/daos_agent.yml /etc/daos/    # access_points, transport_config, fabric_ifaces
mkdir -p /var/run/daos_agent           # ★ 없으면 소켓 bind 실패
systemctl start daos_agent             # 또는 daos_agent -o /etc/daos/daos_agent.yml

daos pool query <pool>                 # 확인
python3 -c "import ctypes; ctypes.CDLL('libdfs.so')"   # 커넥터가 보는 것과 같은 검사
```

- **버전은 서버와 맞춘다.** 클라이언트가 서버보다 앞서면 wire 호환이 깨진다. 이 저장소의
  측정에 쓴 `2.9.100` 은 upstream 배포가 아니라 소스 빌드다 — 릴리스 RPM 은 v2.8 까지다.
- **`--allowerasing` 이 필요한 이유**는 DAOS 가 `libisa-l_crypto`·`mercury` 를 자기 버전으로
  올리려 하고, MLNX OFED 등이 깔아둔 구버전 `-devel` 패키지와 충돌하기 때문이다. EL9 에서
  `dnf install --assumeno daos-client` 로 확인한 트랜잭션은 `daos-client`+`daos` 설치,
  `libisa-l_crypto`/`mercury*` 업그레이드, `libisa-l_crypto-devel` 제거였다.
- `daos_agent.yml` 의 `fabric_ifaces.domain` 은 **포트 접미사까지** 필요하다
  (`mlx5_0` 이 아니라 `mlx5_0:1`). 접미사 없이 주면 클라이언트가
  `ucp_init() failed (No such device)` 로 죽는다.
- `daos` CLI 는 **에이전트가 있는 클라이언트에서** 실행한다. 서버 노드에서는
  `daos_eq_lib_init` 이 `DER_HG` 로 실패하고, 그쪽에서 도는 것은 `dmg` 다.
- 측정 당시 쓴 RPM 스냅샷은 [`deploy/env/daos-repo.listing.txt`](deploy/env/daos-repo.listing.txt)
  에 있다 — **EL8 / 2.8.0 목록**이므로 EL9 호스트에는 그대로 쓸 수 없다.
- 컨테이너 안에서 서빙한다면 호스트 `/usr/lib64` 를 통째로 bind-mount 하지 말 것(glibc
  충돌로 죽는다). 필요한 라이브러리만 골라 모으는 방법은
  [`deploy/README.md`](deploy/README.md) §4 에 있다.

### 2. 커넥터 패키지

```bash
git clone https://github.com/gluesys/lmcache-daos.git
cd lmcache-daos
pip install .          # 개발 설치는 pip install -e .
```

빌드 산출물이 필요한 컴포넌트는 없다. 순수 파이썬 패키지(`lmcache_daos`,
`lmcache_daos.mp`)이고, in-process 모드와 MP 모드는 이것으로 끝이다. GDS 모드만
별도 빌드가 필요하다 ([아래](#gds-모드-gpu-direct)).

```bash
python3 tests/test_serde.py    # DAOS 없이 되는 확인 — 프레이밍 로직
```

### 3. 선택 — `daos_event_t` ABI shim

event queue 경로는 현재 미사용이므로 PoC 에는 필요 없다. 제품화 시에는 구조체 크기를
C 쪽에서 가져오는 편이 안전하다:

```bash
gcc -O2 -fPIC -shared -o libdaos_evshim.so shim/daos_evshim.c -ldaos
export DAOS_EVSHIM_PATH=$PWD/libdaos_evshim.so
```

## 사용법 (in-process 모드)

### 1. DAOS 컨테이너 준비

```bash
daos cont create <pool> <container> \
    --type POSIX --file-oclass=S16 --chunk-size=4194304 --properties=rd_fac:0
```

`--chunk-size=4194304`(**4 MiB**)가 성능의 대부분을 결정한다. 기본 1 MiB 는 per-chunk
RPC 오버헤드로 read 를 크게 떨어뜨린다. 대략 `파일크기 ÷ 랭크당 타깃수` 를 목표로
한다. oclass·복제 계수는 read 성능에 영향이 없었다.

### 2. LMCache 설정

[`examples/lmcache_daos.yaml`](examples/lmcache_daos.yaml) 을 복사해서 쓴다:

```yaml
chunk_size: 256
remote_url: "plugin://daos/<pool>/<container>"
remote_serde: "naive"
remote_storage_plugins: ["daos"]
extra_config:
  remote_storage_plugin.daos.module_path: lmcache_daos.connector
  remote_storage_plugin.daos.class_name: DaosConnector
```

### 3. 실행

```bash
export PYTHONHASHSEED=0                              # 필수 — 아래 참고
export LMCACHE_CONFIG_FILE=examples/lmcache_daos.yaml
vllm serve <model> --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'
```

컨테이너 이미지·bind mount·런처를 포함한 실제 기동 예시는
[`deploy/launchers/`](deploy/launchers/) 와 [`deploy/README.md`](deploy/README.md) 에 있다.

### URL 규칙 (중요)

LMCache 는 out-of-tree `RemoteConnector` 를 `DynamicConnectorAdapter` 로 자동 래핑하고,
그 어댑터의 스킴은 `plugin://<plugin_type>` 이며 `can_parse()` 는 `startswith()` 검사다.
따라서:

```
plugin://<plugin_name>/<pool>/<container>[?sys=<sysname>]
```

- **`daos://<pool>/<container>` 는 동작하지 않는다.** 어떤 어댑터에도 매칭되지 않아
  `CreateConnector` 가 `No adapter found for URL: daos://...` 로 실패한다.
- 어댑터는 생성자에 `url` 을 넘기지 않으므로, 커넥터는 대상 pool/container 를
  `config.remote_url` 에서 얻는다.
- 플러그인명은 `{type}` 또는 `{type}.{instance}` 형식이다(`daos.nvme` 처럼 인스턴스
  분리 가능 — 스킴에는 `.` 앞부분만 쓰인다).

### `PYTHONHASHSEED` 고정 (필수)

프로세스·노드 간 캐시 공유에는 `PYTHONHASHSEED` 를 고정해야 한다. LMCache 가 vLLM 의
해시 함수를 못 불러오면 Python builtin `str` hash 로 폴백하고, 이 해시는 프로세스마다
salt 가 달라 **같은 prompt 가 다른 청크 키를 만든다**. 결과적으로 재시작 후 hit 이 0 이
된다. 엔진 기동 **전에** 설정하고, cross-node 구성에서는 모든 노드에 동일 값을 줄 것.

그 밖의 실환경 함정(libdfs 규칙, `DER_NOSPACE`, vLLM 프로세스 누수, FlashInfer JIT 등)은
[`doc/DESIGN-AND-VALIDATION.md`](doc/DESIGN-AND-VALIDATION.md) 의 "운영 주의사항" 절에
정리돼 있다.

## 다른 동작 모드

위의 in-process `DaosConnector` 가 기본 경로다. 같은 저장소에 두 개의 다른 데이터 평면이
더 있고, **MP 는 추가 빌드가 없고 GDS 만 컴파일이 필요하다.**

### MP 모드 (multiprocess)

별도 프로세스의 LMCache 캐시 서버가 L1(pinned CPU)과 L2 를 소유하고, DAOS 는
`RemoteConnector` 가 아니라 `L2AdapterInterface`(eventfd 기반 비동기 배치 계약)로 붙는다.
여러 vLLM 인스턴스가 한 캐시 서버를 공유할 수 있다.

**빌드**: 없다. `pip install .` 로 들어가는 `lmcache_daos.mp` 가 전부이고, DAOS 요구사항은
in-process 와 같다(stock 클라이언트).

**실행** — 한 호스트에서 두 프로세스를 띄운다:

```bash
# 1) 캐시 서버 (L1 + DAOS L2 어댑터). 어댑터 타입 "daos" 는 우리 엔트리포인트가 등록한다
python3 -m lmcache_daos.mp.server --host localhost --port 5555 --chunk-size 256 \
    --l1-size-gb 100 --eviction-policy LRU \
    --max-gpu-workers 4 --max-cpu-workers 8 \
    --l2-adapter '{"type":"daos","pool":"<pool>","container":"<cont>","root":"/mp","workers":8}'

# 2) 포트가 열린 뒤 vLLM
vllm serve <model> --kv-transfer-config '{"kv_connector":"DaosMPConnector",
  "kv_connector_module_path":"lmcache_daos.mp.vllm_connector","kv_role":"kv_both",
  "kv_connector_extra_config":{"lmcache.mp.host":"tcp://localhost","lmcache.mp.port":5555}}'
```

알아둘 것:

- **`LMCACHE_CONFIG_FILE` 을 쓰지 않는다.** MP 모드의 설정은 서버 인자다.
- **`kv_connector` 이름이 `DaosMPConnector` 인 이유**는 DAOS 와 무관하다. vLLM 0.18 은
  등록된 `LMCacheMPConnector` 를 `kv_connector_module_path` 보다 먼저 해석해 번들된 낡은
  사본을 집고 `ZMQError: Invalid argument (addr='t')` 로 죽는다.
  `lmcache_daos/mp/vllm_connector.py` 는 LMCache 자신의 커넥터를 등록되지 않은 이름으로
  다시 노출해 이를 우회한다.
- **키 네임스페이스가 in-process 와 호환되지 않는다.** MP 모드는 서버가 blake3 로 토큰
  해시를 계산한다. `root`(위 예의 `/mp`)로 컨테이너 안에서 분리해 두는 것이 안전하다.
- 파일 프레이밍도 다르다 — fs 어댑터처럼 **raw 페이로드만** 쓰고 절단 판정은 크기 비교로
  한다(`verify_size`). 쓰기는 tmp → `dfs_sys_rename`.
- 기동 시 모든 타깃에 SX 프로브로 연결을 미리 맺는다(`probe_chunks`, 기본 64). 이것이
  없으면 store 직후 첫 대용량 load 가 ~15 s 멈추는 현상이 난다.

컨테이너 하나에서 두 프로세스를 관리하는 예시는
[`deploy/launchers/run_vllm_mp_c5.sh`](deploy/launchers/run_vllm_mp_c5.sh),
두 vLLM 인스턴스가 한 서버를 공유하는 예시는 `run_vllm_mp2_c5.sh` 다.
설계·계약·측정 결과는 [`doc/MP-MODE-PLAN.md`](doc/MP-MODE-PLAN.md) 와
[`deploy/README.md`](deploy/README.md) §11 에 있다.

### GDS 모드 (GPU-direct)

`dfs_read_gpu`/`dfs_write_gpu` 로 KV 청크를 DAOS ↔ GPU 메모리에 직접 읽고 쓴다. 호스트
DRAM 이 데이터 경로에서 빠진다. LMCache 의 `RemoteConnector` 가 아니라 storage plugin
(`DaosGdsBackend`)으로 붙고, 온디스크 포맷은 v2(`serde_v2`, `[4 KiB 헤더 페이지][payload]`)다.

**여기만 빌드가 필요하다.** upstream DAOS 초안(`theodore/b_cufile`)은 그대로는 빌드조차
되지 않아 이 저장소의 패치 7개가 필요하다. 서버에는 CUDA 가 필요 없고, **stock 서버를
그대로 두고 클라이언트만 교체해도 동작한다**(Phase 1 확인).

```bash
# 0) 초안 브랜치 + 서브모듈 (raft 서브모듈 없이 클론하면 빌드가 깨진다)
git clone -b theodore/b_cufile --recurse-submodules \
    https://github.com/daos-stack/daos daos-gds

# 1) DAOS 트리 패치
gpudirect/apply-patches.sh daos daos-gds

# 2) 서버 빌드 (CUDA 불필요)
cd daos-gds && scons --jobs "$(nproc)" --config=force --build-deps=yes \
    install PREFIX=/opt/daos-gds

# 3) GPU 클라이언트 빌드
scons --jobs "$(nproc)" --config=force --build-deps=yes install \
    BUILD_GPU_DIRECT=yes PREFIX=/opt/daos-gds-gpu BUILD_ROOT=$PWD/build-gpu

# 4) 상류 패치(UCX·Mercury·libfabric) → 세 컴포넌트만 수동 재빌드
../gpudirect/apply-patches.sh deps daos-gds "$PWD/build-gpu"
```

**순서를 지켜야 한다.** `scons --build-deps=yes` 는 prereq git 트리에 `git reset --hard`
를 하므로, 4단계 뒤에 scons 를 다시 돌리면 세 패치가 **조용히 사라진다** — 빌드는 성공하고
런타임에 `ucp_mem_map()` 이 `-EINVAL` 을 낼 뿐이라 알아채기 어렵다. libfabric 은
`--with-cuda` 로 재configure 해야 dmabuf 경로가 살아난다.

빌드 호스트에 필요한 CUDA 패키지(GPU 는 불필요 — 서버 노드에서 빌드해도 된다):

```
cuda-cudart-devel-13-3   cuda-driver-devel-13-3   cuda-nvcc-13-3   cuda-nvml-devel-13-3
```

gdrcopy 는 배포 repo 에 없어 소스 빌드가 필요하다
(`make CUDA=/usr/local/cuda prefix=/usr/local lib lib_install`).

**런타임 요구사항** (하나라도 빠지면 GPU 버퍼 등록이 실패한다):

- **nvidia open 커널 모듈** (`kmod-nvidia-open-dkms`). closed 모듈은 dma-buf export 를
  거부한다 — `DMA-BUF is not supported on this GPU` 가 그 증상이다.
- 클라이언트에 **`libcudart.so.13` 과 `libgdrapi.so.2`** 가 있어야 한다(UCX 를 빌드한
  호스트에만 있으면 안 된다).
- **replicated oclass 컨테이너.** 초안은 EC 객체에 `-DER_NOTSUPPORTED` 를 낸다:
  `daos cont create <pool> <cont> --type POSIX --oclass=RP_2G1 --dir-oclass=RP_2G1 --file-oclass=RP_2G1`
- 에이전트 fabric domain 에 **포트 접미사**(`mlx5_0:1`).
- env: `LMCACHE_DAOS_LIBDIR=/opt/daos-gds-gpu/lib64`, `D_MEM_DEVICE=1`, `D_GPU_DIRECT=1`,
  `LD_LIBRARY_PATH` 선두에 CUDA libfabric(`/opt/ofi-cuda/lib64`), `--ulimit nofile=65536`.
- `peermem` 은 불필요하다(DOCA 3.4 에서는 적재 자체가 불가능하다).

**LMCache 설정**:

```yaml
chunk_size: 256
local_cpu: false                 # CPU 백엔드는 allocator 로만 쓴다
storage_plugins: ["daosgds"]
extra_config:
  storage_plugin.daosgds.module_path: lmcache_daos.gds_backend
  storage_plugin.daosgds.class_name: DaosGdsBackend
  daosgds.pool: <pool>
  daosgds.container: <cont>      # replicated oclass, chunk 4194304
  daosgds.gpu_buffer_gb: 6       # GPU 스테이징 풀 — inflight × KV 크기 이상
  daosgds.io_workers: 16
enable_async_loading: False      # True 면 lookup 시점 prefetch 로 요청 간 겹침
```

`kv_connector` 는 in-process 와 같은 `LMCacheConnectorV1` 을 쓴다. 런처 예시는
[`deploy/launchers/run_vllm_gds_c5.sh`](deploy/launchers/run_vllm_gds_c5.sh).

**빌드 검증**은 세 단계로 좁혀 들어간다 — 위에서 실패하면 아래는 볼 필요가 없다:

```bash
gcc -O2 -o dmabuf_mr    tests/dmabuf_mr.c    -libverbs -lcuda        # 플랫폼 능력만 (DAOS 배제)
gcc -O2 -o ucp_cuda_reg tests/ucp_cuda_reg.c -include string.h \
    -I$U/include -L$U/lib64 -lucp -lucs -luct -lcuda -Wl,-rpath,$U/lib64   # UCX 만
gcc -O2 -o dfs_gpu_rt   tests/dfs_gpu_rt.c   -I$P/include -L$P/lib64 \
    -ldaos -ldfs -lgurt -lcart -luuid -lcuda -pthread -Wl,-rpath,$P/lib64  # 전체 경로 왕복
```

패치별 증상·원인, 정합성 이슈, 측정 결과는 [`gpudirect/README.md`](gpudirect/README.md) 에
있다.

### completion-ordered 스트리밍

`lmcache_daos/streaming.py`. `read ⊕ H2D` 직렬 합성을 겹쳐 1.55x(33.7 GB/s). 추가 빌드는
없지만 **실제 서빙 반영은 LMCache 상류에 streaming API 가 열려야 한다.** MP 모드에서는
서버가 L2 load 완료를 태스크 단위로 받으므로 구조적으로 겹침이 가능하다.

## 테스트

DAOS 없이 (serde 프레이밍 로직):

```bash
python3 tests/test_serde.py
```

DAOS 가 있는 박스에서:

```bash
daos cont create <pool> <cont> --type POSIX
export DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont>

python3 tests/test_dfs_roundtrip.py        # T1  DFS 왕복 (LMCache 불필요)
python3 tests/test_connector_roundtrip.py  # T2  커넥터 왕복
python3 tests/test_plugin_routing.py       # T3  플러그인 라우팅 + daos:// 거부
python3 tests/test_partial_object.py       # T4  잘린 객체 → 오탐 hit 없음
python3 tests/test_concurrent_writers.py   # T5  동일 키 동시 writer
python3 tests/test_list_and_remove.py      # T7  list() + remove_sync()
python3 tests/test_batched.py              # T8  batched_get / batched_put
python3 tests/test_manyread.py             #     무결성 재현기 (28 MB × 30)
```

T2~T5 는 멱등하다 — 시작 시 대상 키를 제거하므로 반복 실행할 수 있다.

GPU + vLLM 이 있는 박스에서 (E2E):

```bash
POOL=<pool> CONT=<cont> bash tests/phase3_vllm_e2e.sh       # miss → store → 재시작 → hit
POOL=<pool> CONT=<cont> bash tests/phase3_multi_replica.sh  # replica 2개가 공유 L2 재사용
```

마이크로벤치와 클라이언트측 측정 하네스는 각각 `tests/bench_*.py` 와
[`bench/README.md`](bench/README.md) 에 있다.

## 저장소 구조

```
lmcache_daos/     커넥터 본체 (connector / dfs_binding / serde / streaming / gds_backend)
                    mp/  LMCache MP 모드용 L2 어댑터 + vLLM 커넥터
shim/             daos_evshim.c — sizeof(daos_event_t) 를 C 쪽에 두는 선택적 shim
examples/         LMCache 설정 예시
tests/            게이트 테스트 + 마이크로벤치 (DAOS 필요)
bench/            클라이언트측 측정 하네스 (vLLM+LMCache E2E)
deploy/           환경 재구성 — 런처·Containerfile·설정·호스트 스냅샷
gpudirect/        dfs_*_gpu() 스택 — DAOS/UCX/Mercury 패치와 3단 검증 도구
doc/              설계·검증 기록, 그림, 상류 제출 초안
```

## 문서

| 문서 | 내용 |
|---|---|
| [`doc/DESIGN-AND-VALIDATION.md`](doc/DESIGN-AND-VALIDATION.md) | **전체 기록** — 설계 근거, 실측 검증 결과, 운영 주의사항, 미해결 지점 |
| [`deploy/README.md`](deploy/README.md) | 환경 재구성 가이드 + 측정 전 체크리스트 (모르면 결과가 조용히 무효가 된다) |
| [`gpudirect/README.md`](gpudirect/README.md) | GPU-direct 스택의 패치와 검증 절차 |
| [`doc/MP-MODE-PLAN.md`](doc/MP-MODE-PLAN.md) | MP 모드 L2 어댑터 설계·결과 |
| [`doc/MP-VS-HUB-BENCHMARK.md`](doc/MP-VS-HUB-BENCHMARK.md) | MP 모드 vs 기존 벤치마크 재측정 |
| [`doc/lmcache-mp-l2-assessment.md`](doc/lmcache-mp-l2-assessment.md) | LMCache MP 모드 L2 어댑터 평가 |
| [`doc/upstream/`](doc/upstream/) | 상류(libfabric / LMCache / DAOS)에 제출할 초안 |
| [`bench/README.md`](bench/README.md) | 측정 하네스 색인과 판정 기준 |

## 현황과 한계

기능·정합성 게이트(T0–T8, Phase 3)는 전부 PASS 이고 cross-node 공유까지 검증됐다.
남은 것:

- **용량 정책이 없다.** `list()`/`remove_sync()` 로 수단은 갖췄지만 무엇을 언제 지울지는
  미정이다. 그대로 두면 컨테이너가 단조 증가한다.
- **`list()` 가 돌려주는 이름은 `CacheEngineKey` 로 되돌릴 수 없다.** `_key_to_path` 가
  sha256 해싱이라 64자 다이제스트가 나온다. 용량 작업에는 충분하지만 이름에서 키를
  복원하는 소비자에는 못 쓴다.
- **`put()` 이 드물게 `EINVAL`.** 여러 핸들이 같은 부모 디렉터리에 동시 create 할 때
  발생한다(10회 × 16스레드에서 1/160). 재시도는 아직 없다.
- **retrieve 상한은 `read ⊕ H2D` 직렬 합성**이다. 스트리밍으로 1.55x 를 확보했지만
  서빙 반영은 LMCache 상류 API 대기 중이다.
- 다중 클라이언트 메타데이터 경합, rank 증설 시 선형 확장성은 미측정.

전체 목록과 각 항목의 근거는
[`doc/DESIGN-AND-VALIDATION.md`](doc/DESIGN-AND-VALIDATION.md) 의 "알려진 미해결 지점"
절에 있다.

## 기여

이 저장소는 사내 GitLab(`exastor/lmcache-daos`)이 상류이고, `main` 은 GitHub
[`gluesys/lmcache-daos`](https://github.com/gluesys/lmcache-daos) 로 push mirror 된다.
**GitHub 쪽에 직접 푸시하지 말 것** — 미러가 덮어쓴다.

문서에 나오는 IP 주소는 모두 문서용 대역(RFC 5737 / RFC 2544)으로 치환돼 있다. 호스트
suffix 는 원본과 같아 문서 안의 상호 참조는 그대로 유효하다.
