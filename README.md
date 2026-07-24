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
- [ ] **Phase 2 잔여** `list()`(readdir) 구현, 플러그인 스킴 라우팅 실동작 확인.
- [ ] **Phase 3** vLLM+LMCache miss→store→hit, 멀티워커/멀티노드 공유 검증.
- [ ] **Phase 4** 벤치·튜닝(oclass EC/RP, chunk, in-flight), 디렉터리 fanout, RDMA.
- [ ] **Phase 5** eviction/용량관리, SRPM/CI 연계, 문서·HA.

## 테스트

로컬(DAOS 불필요) — serde 로직:
```bash
python3 tests/test_serde.py
```

런타임 박스(DAOS 필요) — DFS roundtrip:
```bash
daos cont create <pool> <cont> --type POSIX
DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> python3 tests/test_dfs_roundtrip.py
```

## 알려진 미해결 지점

- 코드는 v0.5.2 소스 기준으로 작성됐으나 **실제 설치본에서 미검증** — 런타임
  박스에서 import/등록/roundtrip 실동작 확인 필요.
- 외부 플러그인 커넥터의 `remote_url` 스킴 접두어(`daos://` vs 플러그인명 vs
  `external://`)가 `DynamicConnectorAdapter` 라우팅과 정확히 어떻게 매칭되는지
  실측 확인 필요. 현재 파서는 `daos://<pool>/<container>`를 가정.
- `list()`는 현재 `[]` (readdir 마샬링 미구현).

## 실환경에서 확인된 libdfs 규칙 (DAOS 2.8/2.9 빌드)

- `dfs_sys_connect` 전에 `daos_init()` **및 `dfs_init()`** 를 모두 호출해야 함
  (dfs_init 누락 시 EACCES=13). 바인딩에 반영됨.
- `dfs_sys_remove`는 이 빌드에서 **ENOTSUP(95)** → `dfs_sys_remove_type(path,
  force=False, mode=0, NULL)` 사용. `force=True`도 ENOTSUP 유발 → False 필수.
- 서버측: `daos_server` 유저가 kdev NVMe 블록디바이스(`root:disk 660`)를 열려면
  `usermod -aG disk daos_server` 필요 (안 그러면 bdev_aio_open Permission denied
  → 엔진 기동 실패). 자세한 클러스터 운영 메모는 세션 메모리 참고.
