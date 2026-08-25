# 클라이언트측 측정 하네스

client-6 의 `/root/lmc/` 에 있던 것들이다(머신 반납에 대비해 옮김).
모두 **vLLM OpenAI 호환 API(`http://localhost:8001`)를 두드리는 방식**이므로,
서빙 컨테이너 안에서 실행하거나 포트가 보이는 곳에서 실행한다. 런처는
`../deploy/launchers/run_arm_yarn.sh` 를 쓴다.

```bash
podman exec -e ARM=daos -e ... vllm-daos python3 /cfg/<script>.py
```
(런처가 이 디렉터리를 컨테이너의 `/cfg` 로 마운트한다.)

## 주력 (결과 문서에 인용된 것)

| 스크립트 | 용도 | 주요 env |
|---|---|---|
| **`sweep2.py`** | 컨텍스트 스윕 8K–127K. arm 별 miss/hit TTFT | `ARM` `CTXS` |
| **`longdocqa.py`** | VAST long-doc-qa 재현 + 멀티노드 크로스노드 검증 | `ARM` `NDOC` `DOCTOK` `INFLIGHT` `NQ` `POPULATE` |
| **`bench_value.py`** | recompute vs DAOS hit TTFT — **성능 판정의 주지표** | — |
| **`bench_scale_tok.py`** | 사전토크나이즈 멀티요청 스케일 | — |
| **`loadgen.py`** | CPU 사용률 측정용 지속 부하 | — |
| **`h2d.py`** | pinned vs 비pinned host→device 대역폭 (torch 단독) | — |

### 두 가지 중요한 설계 결정

- **`sweep2.py` 는 `sweep.py` 의 교정판이다.** `sweep.py` 는 매 요청마다 텍스트를
  다시 만들어 토큰열이 흔들렸다. `sweep2.py` 는 목표 토큰 수까지 **증분 토크나이즈해
  배열을 한 번만 만들고 고정**하며, KV 크기에 비례해 settle 을 기다리고,
  `hit > miss/2` 면 재시도한 뒤 그래도 초과하면 **INVALID 로 표시**한다.
  스윕 결과를 다시 뽑을 때는 `sweep2.py` 를 쓴다.
- **긴 프롬프트는 반드시 `prompt_token_ids` 로 보낸다.** 텍스트로 보내면
  토크나이즈가 API 서버(단일 프로세스/GIL)에서 직렬화되어 TTFT 를 지배한다
  — 13.7K 토큰에서 4.2× 왜곡을 실측했다. `sweep2.py`/`longdocqa.py`/`bench_scale_tok.py`
  는 이미 그렇게 되어 있다.

### `longdocqa.py` 사용 예

```bash
# Part B: 100 GB working set 을 채우고 12 inflight 로 질의
podman exec -e ARM=daos -e NDOC=149 -e DOCTOK=4096 -e INFLIGHT=12 -e NQ=149 \
    vllm-daos python3 /cfg/longdocqa.py

# Part C: 다른 노드에서 질의만 (populate 생략) -> 크로스노드 재사용 검증
podman exec -e ARM=daos-c7 -e POPULATE=0 -e NDOC=149 -e DOCTOK=4096 \
            -e INFLIGHT=12 -e NQ=149 vllm-c7 python3 /cfg/longdocqa.py
```

코퍼스는 고정 시드(`longdocqa_v1`)로 생성되므로 **arm 간·노드 간 동일**하다.
크로스노드 히트에는 모델경로·`served-model-name`·`chunk_size`·TP·`PYTHONHASHSEED`·
MML/YaRN factor 가 노드 간 완전히 일치해야 한다(`../deploy/README.md` §7).

## 초기 탐색용 (역사적 기록)

`bench2.py` `bench_conc.py` `bench_daos.py` `bench_final.py` `bench_fresh.py`
`bench_long.py` `bench_roce.py` `bench_scale.py` `bench_tiny.py` `sweep.py`

병목을 찾아가는 과정에서 쓴 일회성 스크립트들이다. 남겨두는 이유는 당시 수치의
출처를 추적할 수 있어야 하기 때문이고, **새 측정에는 위 "주력" 쪽을 쓴다.**
특히 이들 중 일부는 텍스트 프롬프트를 쓰거나 토큰열을 고정하지 않아
위에 적은 왜곡을 그대로 안고 있다.

## 판정 기준

- **성능 판정은 클라이언트 wall-clock TTFT 로만** 한다.
  `enable_async_loading` 하에서 LMCache 가 보고하는 throughput 은 동기 구간만
  계상해 과대 보고한다(측정된 raw 상한을 넘는 값이 나온다).
- 캐시 히트 여부는 컨테이너 로그의 `LMCache hit tokens: <n>` 으로 확인한다.
  `max_local_cpu_size` 가 부족하면 **에러 없이 `hit tokens: 0`** 이 된다
  (`../deploy/README.md` §7 체크리스트).
