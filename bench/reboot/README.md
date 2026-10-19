<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# 저장 노드 재부팅 시험

vLLM 이 KV 캐시를 DAOS 에 두고 요청을 받는 동안 저장 노드 하나의 전원을 끊었다
켜서, rank 가드(`DAOS_RANK_GUARD=1`)가 요청을 멈추지 않게 하는지 잰다.
`../../doc/RANK-GUARD.md` §5 의 표(run K·P·O·T)가 이 스크립트로 나온 결과다.

## 필요한 것

- daos-operator 로 띄운 DAOS 시스템. 저장 노드 3 개(rank 3 개), 엔진은 노드당 하나.
  `daos-control`·`daos-agent` ConfigMap 과 CSI 노드 파드를 operator 가 만든다.
- GPU 노드 하나. `nvidia` 런타임 핸들러와 DAOS 패브릭(IB 면 `/dev/infiniband`)이 있어야 한다.
- vLLM·LMCache·DAOS 클라이언트 라이브러리가 든 이미지(`VLLM_IMAGE`). 시험은
  vLLM 0.30 + LMCache 0.5.5 + DAOS 2.8.0 으로 했다. lmcache-daos 는 이미지에 넣지 않고
  이 체크아웃을 GPU 노드의 `/opt/lmcd` 로 복사해 `PYTHONPATH` 로 쓴다.
- 저장 노드의 전원을 끄고 켜는 명령(`POWER_OFF`, `POWER_ON`). 깨끗한 종료가 아니라
  전원 차단이어야 한다.
- 이 스크립트를 돌리는 곳에서 `kubectl`, `ssh $GPU_SSH`, `envsubst`, GPU 노드의 8100 포트.

## 설정

`env.sh` 가 읽는 값이다. 환경변수로 주거나 파일에 적어 `REBOOT_ENV=<파일>` 로 넘긴다.

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `GPU_NODE` | (필수) | vLLM 을 띄울 노드 이름 |
| `GPU_SSH` | `root@$GPU_NODE` | 모델·코드 복사와 vLLM 로그 수집에 쓰는 ssh 대상 |
| `VLLM_URL` | `http://$GPU_NODE:8100` | 시험 클라이언트가 두드릴 주소 |
| `VLLM_IMAGE` | (vllm 단계에 필수) | vLLM 이미지 |
| `VLLM_MANIFEST`, `VLLM_POD` | `vllm-guard.yaml`, `vllm-guard` | MP 모드는 `vllm-mp.yaml`, `vllm-mp` |
| `POOL`, `CONT` | `kvt`, `vllm` | KV 캐시를 둘 풀과 컨테이너. EC 시험은 `POOL=kvec` |
| `GUARD_OCLASS` | `S1` | EC 풀이면 `EC_2P1G1` |
| `GUARD_CAPACITY_GB` | (없음) | 축출 상한. 풀의 1/3 이하로 |
| `POWER_OFF`, `POWER_ON` | (재부팅 시험에 필수) | `%s` 자리에 노드 id 가 들어간다 |
| `NS`, `DAOS_SYS` | `daos-system`, `daos_server` | |
| `AGENT_IMAGE`, `ADMIN_IMAGE` | ghcr.io/gluesys DAOS 2.8.0 이미지 | |
| `MODEL_DIR` | `/mnt/hf_cache` | GPU 노드의 모델 디렉터리 |

Proxmox VM 이면 `POWER_OFF='qm stop %s --timeout 120' POWER_ON='qm start %s'`,
BMC 가 있으면 `ipmitool ... chassis power off` 같은 명령을 쓴다.

## 순서

```bash
export GPU_NODE=... VLLM_IMAGE=... POWER_OFF=... POWER_ON=...
bench/reboot/setup.sh admin pools code vllm     # 풀 kvt(rd_fac 0, self_heal none)·kvec(rd_fac 1)
dmg system query -v                              # rank 와 노드 대응을 확인(설치마다 바뀐다)
bench/reboot/reboot.sh out/k1 <rank> <노드 id>   # 약 15 분
```

| 스크립트 | 하는 일 | 결과 파일 |
|---|---|---|
| `reboot.sh <out> <rank> <노드 id>` | 예열 60 s 뒤 전원 차단, 240 s(`DOWN_SECS`) 뒤 전원 투입, Joined 까지 감시 | `ttft.txt`, `states.log`, `v.log` |
| `agg.sh <out> <풀> <rank> <노드 id>` | 같은 장애 동안 살아 있는 rank 의 VOS aggregation·GC 카운터(`daos_metrics`) | `agg.log` |
| `restart.sh <out> <rank> <노드 id>` | 노드를 끈 상태에서 vLLM 을 새로 띄워 기동 시간을 재고, 복귀 뒤 캐시가 스스로 켜지는지 본다 | `ttft.txt`, `states.log`, `v.log` |
| `reint.sh <out> <풀> <rank>` | rd_fac 1 이상 풀에서 제외된 rank 를 부하 중에 재편입하고 rebuild 를 기록 | `ttft.txt` |

`ttft.txt` 는 `tests/vllm_reboot_ttft.py` 의 출력이다. 반복 요청(hit 대상)과 새 요청
(재계산 기준)의 TTFT 를 구간별로 적는다. `PQ=<풀>` 을 주면 `states.log` 에 그 풀의
비활성 target 과 rebuild 상태가 같이 남는다.

## 볼 것

- 실패 요청 수(`ttft.txt` 끝의 오류 수)는 0 이어야 한다.
- 장애 중 반복 요청의 p50 이 새 요청(재계산)보다 크지 않아야 한다.
- rd_fac 0 풀은 rank 가 Joined 된 뒤 수 초 안에 hit 가 돌아와야 한다.
- `restart.sh`: vLLM 기동 시간이 정상 때와 같아야 하고, rank 가 Joined 된 뒤 수 초 안에
  `S1BucketStore ready` 가 로그에 나와야 한다. 그 전까지는 모든 요청이 miss 다.
- EC 풀은 장애 직후 60~90 s 동안 재계산 수준 miss 가 나온다. DAOS 가 degraded 로
  다시 빨라지는 시간이며, breaker 상한을 줄여도 짧아지지 않는다.
- `self_heal:none` 풀은 rank 가 내려가 있는 동안 살아 있는 rank 의 aggregation·GC 도
  멈춘다(`agg.sh`). 용량 상한을 잡을 때 이 시간만큼의 쓰기를 더 감안한다.

## 주의

- `dmg storage query` 는 쓰지 않는다. 일부 구성에서 DAOS 2.8 서버를 죽인다.
- 재부팅한 rank 가 합류 직후 다시 dead 로 판정돼 Excluded 로 남는 일이 있다(18 회 중
  2 회). 엔진 파드를 재시작하면 돌아온다.
- 풀 생성의 `--tier-ratio 12` 는 14 GiB, 노드당 4 target 기준이다. 크기나 target 수가
  다르면 `TIER_RATIO` 를 맞추거나 daos-operator 의 `spec.profile: kvcache` 풀을 쓴다.
