# 환경 재구성 가이드

이 디렉터리는 **client-6 (io500-6) 반납에 대비해** 그 머신에만 있던 자산을 저장소로 옮긴 것이다.
커넥터 코드(`lmcache_daos/`)와 게이트 테스트(`tests/`)는 원래 저장소에 있었지만, 아래 것들은
client-6 의 `/root` 에만 있었다:

| 위치 | 옮긴 곳 |
|---|---|
| `/root/lmc/*.py` | `bench/` — 클라이언트측 측정 하네스 |
| `/root/run_*.sh`, `/root/concur.sh` | `deploy/launchers/` |
| `/root/{ucxbuild,lmcbuild}/Containerfile` | `deploy/containerfiles/` |
| `/root/lmc/*.yaml`, `/etc/daos/daos_agent.yml` | `deploy/config/` |
| 호스트/라이브러리 스냅샷 | `deploy/env/` |

**저장소에 넣지 않은 것**(용량·라이선스 문제, 재생성 가능):

| 항목 | 크기 | 재생성 방법 |
|---|---|---|
| `/root/daoslib-ucx/` | 36 MB | §4 절차로 재큐레이션. 파일 목록은 `env/daoslib-ucx.manifest.txt` (349행) |
| `/root/daos-repo/` (DAOS 2.8 rpm) | 수백 MB | ExaStor 빌드 산출물. 목록은 `env/daos-repo.listing.txt` |
| `kvsup-ucx-lmc:local` 이미지 | 22.4 GB | §5 의 Containerfile 로 재빌드 |
| `/home/hf_cache` (Qwen3-14B) | 28 GB | HuggingFace 에서 재다운로드 |

---

## 1. 측정 당시 토폴로지

`env/host-env.txt` 가 client-6 의 실제 스냅샷이다. 요약:

```
client-6 (io500-6)   Rocky Linux 10.2, podman 5.8.2
                     H100 NVL 95830 MiB, driver 610.57.04
                     ens4f1     10.100.230.6/24    관리
                     ens255np0  192.168.10.60/24   RoCE 400GbE (mlx5_0)

client-7 (io500-7)   Rocky Linux 8.10, podman 4.9.4-rhel   ← Part C 에서 2번째 노드로 사용
                     H100 NVL, driver 610.43.02
                     ens255np0  192.168.10.17/24

cell1  DAOS rank0    ens2 192.168.10.82 (mlx5_0), ens1 192.168.10.81 (mlx5_1)
cell2  DAOS rank1    ens2 192.168.10.84,          ens1 192.168.10.83
```

- 접속 경로: `tta1 → cell1 → client-*`. **client 노드끼리는 직결 SSH 가 없다** — 전송은 cell1 을 릴레이로 쓰고, 속도가 필요하면 RoCE IP(`192.168.10.x`)를 쓴다(실측 ~930 MB/s).
- `/etc/hosts` 상 `client-N` == `io500-N` (.1 ~ .7). **client-5 는 공개키가 등록되어 있지 않아** 사용 불가였다.
- ⚠️ cell 노드의 **OS 디스크는 `nvme24n1`** 이다. `dd if=/dev/zero of=/dev/nvme*n1` 같은 와일드카드는 절대 쓰지 말 것 (한 번 GPT+ESP 를 날려 복구해야 했다).

## 2. DAOS 측 구성

```bash
# 풀 (cell1 에서 dmg, -i 필요)
dmg -i pool query kvpool2
#   SCM  16 GB  (8 GB/rank)      <- 100 GB working set 이 여기 상주할 수 없다는 근거
#   NVMe 400 GB

# 컨테이너 — 이 3개 속성이 성능의 대부분을 결정한다
daos cont create kvpool2 kv2s16 \
    --type POSIX --file-oclass=S16 --chunk-size=4194304 --properties=rd_fac:0
```

- `--chunk-size=4194304` (**4 MiB**)가 핵심이다. 기본 1 MiB 는 per-chunk RPC 오버헤드로 read 를 3.8× 떨어뜨린다. 대략 `파일크기 ÷ 랭크당 타깃수` 를 목표로 한다.
- oclass·복제 계수는 read 성능에 영향이 없었다(S1/S8/S16/SX/RP_2G4 비교).
- 풀 생성 시 `--size=%` 나 `--size=4TB` 는 DER_NOSPACE 가 난다 → `--scm-size`/`--nvme-size` 를 명시한다.
- **컨테이너 destroy 후 공간 회수는 지연 반영**된다(aggregation). 대용량 측정 전에는 재생성으로 확보하는 편이 확실하다.
- `daos` CLI 는 **에이전트가 있는 클라이언트에서** 실행해야 한다. cell1(서버)에서는 `daos_eq_lib_init` 이 DER_HG 로 실패한다. `dmg` 는 cell1 에서 동작한다.

전송 provider 는 **UCX (`ucx+rc_v`)** 다. libfabric `verbs;ofi_rxm` 은 대용량 RDMA read 를
**조용히 손상**시킨다(28 MB × 30 중 3–10개만 정상). raw verbs(`ib_write_bw`, `rping -v`)는 무결하므로
libfabric 계층 문제다. UCX 활성화의 실제 관문은 재빌드가 아니라 **패키징에서 빠진
`libna_plugin_ucx.so`** 였다.

## 3. 클라이언트 DAOS 스택

```bash
# rpm 은 env/daos-repo.listing.txt 참고. libpmem 충돌 때문에 --allowerasing 필요
dnf -y --nogpgcheck --allowerasing localinstall \
    $(ls daos-repo/*.rpm | grep -vE "debuginfo|debugsource|tests|server|admin|firmware")

cp config/daos_agent.yml /etc/daos/     # allow_insecure: true -> 인증서 불필요
mkdir -p /var/run/daos_agent            # ★ 없으면 소켓 bind 실패
nohup daos_agent -o /etc/daos/daos_agent.yml > /var/log/daos_agent.log 2>&1 &
daos pool query kvpool2                 # 확인
```

`daos_agent.yml` 의 `fabric_ifaces` 는 **포트까지 명시**해야 한다(`domain: mlx5_0:1`).
바닐라 UCX 는 `mlx5_0` 만 주면 거부한다.

## 4. `daoslib-ucx/` 큐레이션 — 컨테이너에 DAOS 를 주입하는 방법

컨테이너에 호스트 `/usr/lib64` 를 그대로 bind-mount 하면 **glibc 충돌**로 죽는다.
필요한 라이브러리만 골라 별도 디렉터리에 모아 `/daoslib` 로 마운트한다.

```bash
mkdir -p /root/daoslib-ucx/{mercury,libibverbs}
cp -L /usr/lib64/{libdaos,libdfs,libgurt,libcart,libabt,libuuid,...}.so* /root/daoslib-ucx/
cp -L /usr/lib64/mercury/* /root/daoslib-ucx/mercury/        # libna_plugin_ucx.so 포함 확인
cp -L /usr/lib64/libibverbs/* /root/daoslib-ucx/libibverbs/  # mlx5 provider
```

정확한 파일 목록(심볼릭 링크 관계·버전 포함)은 **`env/daoslib-ucx.manifest.txt`** 에 있다.

두 가지 함정:

1. **`cp -L`** 로 심링크를 실체화해야 한다. 그냥 복사하면 컨테이너 안에서 링크가 깨진다.
2. **DT_RELR 재배치를 쓰는 라이브러리는 컨테이너의 오래된 glibc 가 로드하지 못한다.**
   `libprotobuf-c`, `libyaml` 이 여기 해당해서 **cell1 의 EL8 버전으로 교체**했다.

런타임에는 아래를 함께 넘긴다:

```
-e LD_LIBRARY_PATH=/usr/local/cuda-12.9/lib64:/usr/local/cuda-12.9/targets/x86_64-linux/lib:\
/opt/ucx/lib:/daoslib:/daoslib/mercury
-v /etc/libibverbs.d:/etc/libibverbs.d:ro
--device /dev/infiniband --ulimit memlock=-1:-1 --cap-add=IPC_LOCK
```

## 5. 컨테이너 이미지 (2단 빌드)

```bash
podman build -t kvsup-ucx:local     -f containerfiles/ucx.Containerfile     .
podman build -t kvsup-ucx-lmc:local -f containerfiles/lmcache.Containerfile .
```

- **1단 (`ucx.Containerfile`)**: 베이스 이미지에 rdma-core 를 넣고 **UCX 1.20 을 소스 빌드**한다.
  기존 UCX 로는 `ucp_init` 이 "No such device" 로 실패했다.
- **2단 (`lmcache.Containerfile`)**: `lmcache==0.5.2` 를 **소스 빌드**한다
  (`--no-binary :all:`, `CUDA_HOME=/usr/local/cuda-12.9`, `TORCH_CUDA_ARCH_LIST=9.0`).
  휠을 그냥 설치하면 이미지의 torch CUDA 버전과 어긋나 **`c_ops` CUDA 커널이 비활성화되고
  retrieve 가 6× 느려진다**(3.3 vs 20.4 GB/s). 기동 로그에 `lmcache.c_ops` 가 보여야 한다.

**podman/CDI 주의** — client-7(podman 4.9.4)에서 겪은 것:
`nvidia-ctk` 1.20 이 만든 CDI 스펙은 `cdiVersion 0.7.0` + `additionalGids` 를 포함해
podman 4.9.4 가 unknown field 로 거부한다(`unresolvable CDI devices nvidia.com/gpu=all`).
`additionalGids` 를 지우고 `cdiVersion` 을 `0.6.0` 으로 낮추면 된다.
**다른 노드의 CDI yaml 을 복사하면 안 된다** — 드라이버 버전이 경로에 박혀 있다.

## 6. 실행

```bash
# arm 전환형 런처 (recompute / cpu / nvme / daos)
MML=32768 FACTOR=1.0 launchers/run_arm_yarn.sh daos

# 64K/127K 는 YaRN 이 필요하다
MML=66560  FACTOR=1.625 launchers/run_arm_yarn.sh daos   # 64K
MML=131072 FACTOR=3.2   launchers/run_arm_yarn.sh daos   # 127K
```

### 런처 색인 (세대가 여러 개다)

| 파일 | 상태 | 비고 |
|---|---|---|
| **`run_arm_yarn.sh`** | ★ **현행** | arm 전환 + YaRN(`MML`/`FACTOR`). Part A·B·C 전부 이걸로 측정 |
| `run_arm.sh` | client-7 용 | 컨테이너명 `vllm-c7`. Part C 2번째 노드에서 사용 |
| `run_vllm_ucx_14b.sh` | 구세대 | UCX 전환 직후의 14B 단발 런처 |
| `run_vllm_lmc.sh` | 구세대 | `kvsup-ucx-lmc` 이미지 도입 시점 |
| `run_vllm_ucx.sh`, `run_vllm_daos.sh`, `run_vllm_mbt.sh` | 구세대 | 탐색기. `-v /root/daoslibs`(UCX 이전 라이브러리 묶음)를 참조하는 것이 섞여 있다 |

구세대 런처를 그대로 쓰면 안 되는 이유: 마운트 경로가 **`/root/daoslibs`**(UCX 이전)
를 가리키는 것이 있고, `max_local_cpu_size` 같이 나중에 필수로 밝혀진 설정이 빠져 있다.
새로 구성할 때는 `run_arm_yarn.sh` 를 기준으로 삼는다.

런처가 기대하는 호스트 경로(재구성 시 준비 대상):
`/root/daoslib-ucx` · `/root/lmcache-daos` · `/root/lmc`(→ `bench/`) ·
`/home/hf_cache` · `/home/kvlocal` · `/etc/daos` · `/etc/libibverbs.d` · `/var/run/daos_agent`

- arm 전환 시 **직전 컨테이너의 GPU 메모리 해제와 경쟁**해 기동이 실패한다.
  런처의 `sleep 12` 는 부족하고 **30초**를 권한다. 기동 직후 `podman ps -a` 로 확인하는 단계를
  넣지 않으면 ready 루프가 수백 초를 헛돈다.
- 측정 하네스는 `bench/` 에 있다. 주요 것만:
  - `sweep2.py` — 컨텍스트 스윕(8K–127K). 고정 토큰열, KV 비례 settle, INVALID 자동 플래그
  - `longdocqa.py` — VAST 조건 재현 및 멀티노드. `POPULATE=0` 이면 질의 전용(크로스노드 검증용)
  - `bench_value.py` — recompute vs hit TTFT. 성능 판정의 주지표

## 7. 측정 전 체크리스트

여기 적힌 것을 모르면 **결과가 조용히 무효**가 된다.

- [ ] **`max_local_cpu_size >= 최대 컨텍스트 KV × 동시요청수`** (기본 5 GB).
      넘으면 `local_cpu_backend.allocate()` 가 실패하고 **에러 없이 cache miss** 로 처리된다.
      `local_cpu: false` 여도, 로컬 디스크 백엔드여도 적용된다.
- [ ] 기동 로그에 **`lmcache.c_ops`** 가 있는지 (없으면 retrieve 6× 손실)
- [ ] 긴 프롬프트는 **`prompt_token_ids`** 로 전달 (텍스트 토크나이즈가 API 서버 GIL 에서
      직렬화되어 TTFT 를 지배한다. 13.7K 토큰에서 4.2× 왜곡)
- [ ] DFS **chunk 4 MiB**
- [ ] YaRN 은 `--hf-overrides` 에 `rope_scaling` **과 `max_position_embeddings` 를 함께**.
      입력 컨텍스트는 `MML − 256` 이하 (같게 두면 `VLLMValidationError`)
- [ ] 성능 판정은 **클라이언트 wall-clock 으로만**. `enable_async_loading` 하에서 LMCache 가
      보고하는 throughput 은 동기 구간만 계상해 과대 보고한다(raw 상한을 넘는 값이 나온다)
- [ ] 멀티노드 크로스노드 히트에는 **모델경로·served-model-name·chunk_size·TP·
      `PYTHONHASHSEED=0`·MML/YaRN factor 가 노드 간 완전히 일치**해야 한다.
      YaRN 이 다르면 키는 맞아 히트해도 RoPE 가 달라 **KV 값이 틀린다**

### 벤치 하네스 작성 시의 함정 (실제로 가짜 결과를 만들었던 것들)

- **`ctypes` 버퍼의 `.raw` 는 전체를 복사한다**(GIL 보유). 28 MB × 16 워커에서 직렬화되어
  32.8 GB/s 가 5.8 로 보인다 → `memoryview` 를 쓴다(hashlib 은 큰 입력에서 GIL 을 놓는다).
- **셋업을 timed 구간에 남기지 말 것.** `dfs_sys_connect`·EQ 생성·28 MB calloc 이 스레드 수에
  비례해 누적되어 "확장 안 됨" 곡선이 만들어진다. barrier 로 분리한다.
- **배리어 전에 공용 work queue 를 drain 하면** 첫 스레드가 전량 독점한다
  ("제출 순서대로, 7 GB/s" 라는 그럴듯한 가짜 결과).
- **`DFS_SYS_NO_LOCK` 핸들 여러 개가 같은 부모 디렉터리에 동시 create** 하면
  `dfs_sys_open` 이 EINVAL 을 낸다. 생성만 단일 핸들로 직렬화한다(대량 write 는 파일별이라 안전).
- **파이프 수신측 `ssh` 에 `-n` 을 붙이면** stdin 이 막혀 "not a tar archive" 로 조용히 실패한다.

## 8. 재현된 주요 수치

client-6 + cell1/cell2, Qwen3-14B(KV 160 KiB/token), DAOS 2.8 / UCX / 400GbE RoCE 기준.

| 측정 | 값 |
|---|---|
| hit TTFT (8K → 127K) | 151 → 2129 ms (recompute 대비 3.8× → 11.8×) |
| long-doc-qa 100 GB / 12 inflight | avg TTFT 371 ms, 집계 21.36 GB/s (recompute 11.7×) |
| 크로스노드 재사용 (client-7) | 149/149 히트, avg 444 ms — local NVMe 는 83% 미스 |
| 2노드 동시 집계 | 32.8 GB/s |
| 단일노드 raw read (100 GB NVMe 상주) | **34.27 GB/s** (2 GB 대비 −9%) |
| 단일요청 retrieve (현행, read ⊕ H2D 직렬) | 21.4 GB/s |
| 스트리밍 적용 시 | **33.7 GB/s (1.55×)** — `lmcache_daos/streaming.py` |

상세·정정 이력은 Hub 문서 2건에 있다:

- **KV-cache 벤치마크 A·B·C (개정 2)**
  `http://ac2repo.gluesys.com/document-hub/share.html?share=IPNouoNL3C3QIEoKneZ3sFDAMIt18uev`
- **LMCache streaming-get RFC + DAOS async 리팩터링 계획서 (개정 3)**
  `http://ac2repo.gluesys.com/document-hub/share.html?share=Prlhr3a9GQIA9_CouqlfuH-mtV8dMFO_`

## 9. 권고 구성 (요약)

```
DAOS      ucx+rc_v / 2-rank / S16 + DFS chunk 4 MiB + rd_fac:0
LMCache   chunk_size 256 · enable_async_loading: True · max_local_cpu_size >= 100
          소스 빌드(c_ops 활성)
vLLM      --enforce-eager --no-enable-prefix-caching, prompt_token_ids 로 전달
```
