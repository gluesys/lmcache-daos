# lmcache-daos

LMCache의 KV cache 오프로딩 백엔드를 **DAOS**로 구현하는 프로젝트.
LMCache의 `RemoteConnector` 인터페이스에 `daos://` 스킴 커넥터를 붙여, KV 청크를
DAOS DFS(`dfs_sys` API) 네임스페이스의 self-describing 파일로 저장한다.

## 아키텍처

```
vLLM ─ LMCache ─ RemoteBackend ─(daos:// 스킴)─ DaosConnector (connector.py)
                                                   └─ DfsSys ctypes 바인딩 (dfs_binding.py)
                                                        └─ libdfs dfs_sys_* → DAOS Array 객체(파일)
```

- **키 매핑**: `CacheEngineKey` → `sha256` → DFS 경로 (flat, fanout은 Phase 4)
- **객체 레이아웃**: `serde.py` — `[prefix 8B][header][payload]`. 파일이 자기 자신을
  기술하므로 read 시 별도 stat/index 불필요.
- **동시성**: blocking libdfs 호출은 스레드풀 executor로 분리해 asyncio 루프 비블로킹.

## 저장소 구조

```
lmcache_daos/     커넥터 본체
  connector.py      DaosConnector — batched_get / put(zero-copy) / stream_get
  dfs_binding.py    libdfs ctypes 바인딩 (동기 API + async 용 dfs_read/sgl)
  serde.py          [prefix 8B][meta][payload] 프레이밍
  streaming.py      completion-ordered 스트리밍 (스레드풀 기반) — 1.55× 오버랩
  daos_event.py     DAOS event queue 바인딩 ※ 경로 밖. deploy/README §8 참조
shim/             daos_evshim.c — sizeof(daos_event_t) 를 C 쪽에 두는 선택적 shim
tests/            게이트 테스트 + 마이크로벤치 (DAOS 필요)
bench/            클라이언트측 측정 하네스 (vLLM+LMCache E2E)
deploy/           ★ 환경 재구성 — 런처·Containerfile·설정·호스트 스냅샷
                    client-6 반납에 대비해 그 머신의 자산을 옮겨둔 것
```

**환경을 다시 세우려면 [`deploy/README.md`](deploy/README.md) 를 먼저 읽을 것.**
토폴로지, DAOS 풀/컨테이너 속성, `daoslib-ucx` 큐레이션, 컨테이너 2단 빌드,
그리고 *모르면 결과가 조용히 무효가 되는* 측정 체크리스트가 거기 있다.

## 근거 (검증된 사실)

- DAOS **2.9.100** 소스: `daos/src/include/daos_fs_sys.h`
  (`dfs_sys_connect/_open/_read/_write/_close/_remove`), `daos_fs.h` (플래그).
- Samba **vfs_daos** 모듈이 동일하게 `dfs_sys` API 사용 → 호출 패턴 재사용.
- LMCache `RemoteConnector` 인터페이스: `async exists / exists_sync / async get /
  async put / async list / async close`, 키 타입 `CacheEngineKey`
  (`lmcache/v1/storage_backend/connector/base_connector.py`, dev).

## 대상 버전 핀

**LMCache v0.5.2** 기준으로 구현. 커넥터 생성자/메타데이터 코덱/allocator API를
이 태그 소스에서 직접 확인함. 런타임 박스에서 `pip show lmcache`로 v0.5.2인지
확인 후 진행할 것. 버전이 다르면 `connector.py`의 API 배선을 재확인해야 함.

## 진행 상태

- [x] **Phase 0** 환경 조사 — 이 개발 박스엔 DAOS 런타임/LMCache 미설치.
      실제 I/O는 E2E 런타임 박스에서 (pool/POSIX 컨테이너 필요).
- [x] **Phase 1** libdfs ctypes 바인딩 + serde 프레이밍 + 단위테스트.
      **T1(DFS roundtrip) 실 DAOS 환경 PASS** (클라 192.168.35.40, python3.12,
      pool `lmcache`/cont `lmcache_test`, 1 MiB write/read/remove).
- [x] **Phase 2 (코드)** 실제 LMCache v0.5.2 API로 커넥터 확정:
      생성자 `(url, loop, local_cpu_backend)`, `RemoteMetadata` 메타 코덱,
      `local_cpu_backend.allocate(...)` 재구성, 플러그인 등록 방식.
- [x] **Phase 2 (검증)** 클라 .40에 lmcache==0.5.2(CPU torch) 설치, 커넥터
      import + `_HAS_LMCACHE=True` 확인. **T2 커넥터 왕복 PASS** (LocalCPUBackend
      →put→exists→get, payload/shape/dtype 일치, tests/test_connector_roundtrip.py).
- [x] **Phase 2 잔여** 플러그인 스킴 라우팅 확정 — `plugin://daos/<pool>/<cont>`,
      생성자는 `(loop=, local_cpu_backend=, config=)` 이고 URL 은 `config.remote_url`
      에서 온다. `list()`(readdir)는 여전히 미구현(핫패스에 불필요).
- [x] **Phase 3** vLLM+LMCache miss→store→hit **및 멀티노드 공유 검증 완료.**
      2번째 H100 노드가 첫 노드의 100 GB KV 를 **149/149 전량 히트**(미스 0),
      avg TTFT 444 ms. 동일 조건 local NVMe 는 83% 미스 — 구조적으로 공유 불가.
- [x] **Phase 4** 벤치·튜닝 완료. 결정적이었던 것은 **DFS chunk 4 MiB**(read 9→34.5 GB/s)
      이고 oclass·복제는 무영향. RDMA 는 **UCX** 로 확정(libfabric `verbs;ofi_rxm` 은
      대용량 read 를 조용히 손상). 디렉터리 fanout 은 미구현.
- [x] **Phase 4+** retrieve 파이프라인 — 상한 21.4 GB/s 가 `read ⊕ H2D` **직렬 합성**임을
      규명(모델 오차 0.1%)하고, completion-ordered 스트리밍으로 **33.7 GB/s (1.55×)** 확보.
      실제 서빙 반영은 **LMCache 상류에 streaming API 가 열려야** 한다(RFC 제출 대기).
- [ ] **Phase 5** eviction/용량관리, SRPM/CI 연계, HA.

측정 수치의 전체·정정 이력은 Hub 문서 2건에 있다 — [`deploy/README.md`](deploy/README.md) §8 의 링크 참조.

## 테스트

로컬(DAOS 불필요) — serde 로직:
```bash
python3 tests/test_serde.py
```

런타임 박스(DAOS 필요) — 컨테이너 속성은 `deploy/README.md` §2 참조:
```bash
export DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16

python3 tests/test_dfs_roundtrip.py        # DFS 왕복
python3 tests/test_manyread.py            # ★ 무결성 재현기 28MB×30 — 모든 변경의 전제
python3 tests/test_connector_roundtrip.py  # LMCache 레벨 put/get 정합성
python3 tests/bench_ceiling.py             # raw read 천장
python3 tests/bench_raw_workingset.py      # working set 2→100 GB (NVMe 상주 확인)
```

GPU 가 필요한 것(vLLM 이 GPU 를 점유하므로 **일회용 컨테이너에서** 실행):
```bash
python3 tests/bench_stream_h2d.py          # BATCH vs STREAM 오버랩 (1.55×)
```

event queue 경로(현재 미사용, 근거 보존용):
```bash
python3 tests/test_event_abi.py            # daos_event_t ABI canary + negative control
python3 tests/test_async_manyread.py       # pending table lifecycle, 완료순 매칭
python3 tests/bench_eq_topology.py         # EQ수 × 폴러수 — 왜 안 쓰는지의 근거
```

## 알려진 미해결 지점

- **`list()` 는 `[]`** (readdir 마샬링 미구현). 핫패스에 불필요해 우선순위 낮음.
- **디렉터리 fanout 없음** — 모든 청크가 컨테이너 루트에 평평하게 놓인다.
  대규모 캐시에서 디렉터리 크기가 문제되는지 미측정.
- **LMCache ZMQ RPC 타임아웃** — 대용량 store 를 반복하면 ~6초 주기로 발생. 원인 미규명.
- **컨테이너 `Exited(137)`** arm 전환 시 간헐 발생. 대기 후 재시도로 우회 중.
- **`daos_event_t` ABI** 를 ctypes 로 선언하고 있다(256 B guard + canary 로 방어).
  제품화하려면 `shim/daos_evshim.c` 를 빌드해 `DAOS_EVSHIM_PATH` 로 넘기는 편이 안전하다.
  단 event 경로 자체가 현재 미사용이므로 시급하지 않다.
- **상류 대기**: 스트리밍 이득(1.55×)은 LMCache 에 completion-ordered get API 가
  생겨야 실제 서빙에 반영된다.

## 실환경에서 확인된 libdfs 규칙 (DAOS 2.8/2.9 빌드)

- `dfs_sys_connect` 전에 `daos_init()` **및 `dfs_init()`** 를 모두 호출해야 함
  (dfs_init 누락 시 EACCES=13). 바인딩에 반영됨.
- `dfs_sys_remove`는 이 빌드에서 **ENOTSUP(95)** → `dfs_sys_remove_type(path,
  force=False, mode=0, NULL)` 사용. `force=True`도 ENOTSUP 유발 → False 필수.
- 서버측: `daos_server` 유저가 kdev NVMe 블록디바이스(`root:disk 660`)를 열려면
  `usermod -aG disk daos_server` 필요 (안 그러면 bdev_aio_open Permission denied
  → 엔진 기동 실패). 자세한 클러스터 운영 메모는 세션 메모리 참고.
