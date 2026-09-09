#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Committed as the reproducible Phase 3 harness. Override via env:
#   POOL=nvme_pool CONT=lmcache_nvme PROMPT_TOKENS=4096 MAXLEN=8192 \
#     bash tests/phase3_vllm_e2e.sh
# Phase 3: vLLM + LMCache  miss -> store(DAOS) -> restart -> hit(DAOS)
#
# Restarting vLLM between the two passes is what makes this a real test of the
# DAOS remote tier: it drops the GPU KV cache AND LMCache's local CPU tier, so a
# hit on the second pass can only have come from DAOS.
set -u
# Repo root and venv are overridable so this runs outside the ExaCI5-4 box.
REPO_DIR=${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
VENV=${VENV:-$REPO_DIR/../venv-lmcache}
WORK=${WORK:-${TMPDIR:-/tmp}/lmcache-daos-phase3}
mkdir -p "$WORK"
cd "$REPO_DIR"
# shellcheck disable=SC1091
[ -f "$VENV/bin/activate" ] && source "$VENV/bin/activate"
export HF_HOME=${HF_HOME:-$WORK/hf}

export VLLM_LOGGING_LEVEL=INFO
# FlashInfer JITs its sampling kernel with nvcc; use the torch-native sampler so
# a full CUDA toolchain isn't required just to run the cache test.
export VLLM_USE_FLASHINFER_SAMPLER=0
# MANDATORY for cross-process cache reuse. LMCache derives chunk keys with
# Python's builtin str hash, which is salted per process, so without a fixed
# seed pass 2 computes different keys than pass 1 and can never hit -- LMCache
# logs "Centralized cache sharing detected but PYTHONHASHSEED not set" and
# NONE_HASH differs between the runs.
export PYTHONHASHSEED=0
export CUDA_HOME=${CUDA_HOME:-/usr/local/cuda}
export PATH=$PATH:/usr/local/cuda/bin

MODEL=${MODEL:-Qwen/Qwen3-1.7B}
PORT=8000
POOL=${POOL:-hdd_pool}
CONT=${CONT:-lmcache_test}
MNT=${MNT:-/mnt/lmcache_kv}
PROMPT_TOKENS=${PROMPT_TOKENS:-4096}
GPUUTIL=${GPUUTIL:-0.60}   # A2 = 15 GiB; 1.7B bf16 weights ~3.4 GiB
MAXLEN=${MAXLEN:-8192}
# PASS 1 is only a real MISS if the container starts empty. A rerun against a
# populated container hits on both passes and the cold/warm comparison becomes
# meaningless (measured: 0.669s vs 0.667s = 1.00x). Set PURGE=0 to keep the
# existing objects, e.g. to check cache survival across runs.
PURGE=${PURGE:-1}

# Point LMCache at whichever pool/container this run is measuring.
LMCACHE_CONFIG_FILE=$WORK/lmcache-run.yaml
cat > $LMCACHE_CONFIG_FILE <<EOF
chunk_size: 256
remote_url: "plugin://daos/${POOL}/${CONT}"
remote_serde: "naive"
remote_storage_plugins: ["daos"]
extra_config:
  remote_storage_plugin.daos.module_path: lmcache_daos.connector
  remote_storage_plugin.daos.class_name: DaosConnector
EOF
export LMCACHE_CONFIG_FILE
echo "### pool=$POOL cont=$CONT model=$MODEL prompt_tokens=$PROMPT_TOKENS maxlen=$MAXLEN"

# `daos fs` has no ls; use a dfuse mount (caching off, so counts are current).
mount_dfuse() {
  mkdir -p $MNT
  mountpoint -q $MNT || dfuse --pool $POOL --container $CONT --mountpoint $MNT \
     --disable-caching >/dev/null 2>&1
  sleep 2
}
kv_stat() {
  mountpoint -q $MNT || return
  echo "    files=$(find $MNT -type f 2>/dev/null | wc -l)  bytes=$(du -sb $MNT 2>/dev/null | cut -f1)"
}

start_vllm() {
  local log=$1; rm -f "$log"
  nohup vllm serve "$MODEL" \
    --host 127.0.0.1 --port $PORT \
    --tensor-parallel-size 1 \
    --gpu-memory-utilization $GPUUTIL \
    --max-model-len $MAXLEN \
    --enforce-eager \
    --kv-transfer-config \
      '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
    > "$log" 2>&1 &
  echo $! > "$WORK/vllm.pid"
  for i in $(seq 1 100); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && { echo "  vllm up (~$((i*8))s)"; return 0; }
    pgrep -f "vllm serve" >/dev/null || { echo "  vllm DIED"; tail -25 "$log"; return 1; }
    sleep 8
  done
  echo "  vllm TIMEOUT"; tail -25 "$log"; return 1
}
stop_vllm() {
  [ -f "$WORK/vllm.pid" ] && kill "$(cat "$WORK/vllm.pid")" 2>/dev/null
  pkill -f "vllm serve" 2>/dev/null
  for i in $(seq 1 40); do pgrep -f "vllm serve" >/dev/null || break; sleep 2; done
  # vLLM renames its worker to "VLLM::EngineCore", so `pkill -f vllm` misses it
  # and it keeps holding ~9.5 GiB of the A2's 15 GiB. Reap by GPU ownership --
  # otherwise the next start dies with "Free memory ... less than desired".
  for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do
    kill -9 "$p" 2>/dev/null
  done
  for i in $(seq 1 20); do
    [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null)" ] && break
    sleep 2
  done
  sleep 4
  echo "    gpu after stop: $(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader)"
}

PROMPT=$(python - "$PROMPT_TOKENS" <<'EOF'
import sys
n=int(sys.argv[1])
s=("The distributed object storage subsystem records every transaction in a "
   "write-ahead log before acknowledging the client. ")
out=[]
while len(" ".join(out).split()) < n: out.append(s)
print(" ".join(out))
EOF
)
echo "prompt words: $(echo "$PROMPT" | wc -w)"

ask() {   # $1 = question; prints TTFT seconds
  python - "$PORT" "$PROMPT" "$1" "$MODEL" <<'EOF'
import json,sys,time,urllib.request
port,prompt,q=sys.argv[1],sys.argv[2],sys.argv[3]
body=json.dumps({"model":sys.argv[4],
  "prompt":prompt+"\n\nQuestion: "+q+"\nAnswer:",
  "max_tokens":16,"temperature":0,"stream":True}).encode()
req=urllib.request.Request(f"http://127.0.0.1:{port}/v1/completions",body,
  {"Content-Type":"application/json"})
t0=time.time(); ttft=None
with urllib.request.urlopen(req) as r:
    for line in r:
        if line.startswith(b"data: ") and b"[DONE]" not in line:
            ttft=time.time()-t0; break
print(f"{ttft:.3f}" if ttft else "NA")
EOF
}

lm_evidence() {  # $1 = log
  echo "    NONE_HASH: $(grep -o 'NONE_HASH=[0-9]*' "$1" | head -1)"
  grep -oE "LMCache hit tokens: [0-9]+, need to load: [0-9]+" "$1" | tail -3 | sed 's/^/    /'
  grep -oE "Retrieved [0-9]+ out of total [0-9]+ tokens[^[]*" "$1" | tail -3 | sed 's/^/    /'
  grep -oE "Stored [0-9]+ out of total [0-9]+ tokens[^[]*" "$1" | tail -4 | sed 's/^/    /'
}

trap 'stop_vllm' EXIT   # never leave a worker holding the GPU

mount_dfuse
if [ "$PURGE" = "1" ]; then
  echo "  purging container so PASS 1 is a genuine miss"
  find $MNT -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null
fi
stop_vllm               # clear anything a previous failed run left behind
echo "  gpu at start: $(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader)"
echo "============ PASS 1: cold (expect MISS -> store to DAOS) ============"
echo "  DAOS container before:"; kv_stat
start_vllm "$WORK/vllm-pass1.log" || exit 1
T1=$(ask "summarize in one line")
echo "  TTFT pass1 (cold/miss) = ${T1}s"
sleep 10   # let the async store drain to DAOS
echo "  DAOS container after pass1:"; kv_stat
echo "  --- LMCache evidence (pass1) ---"; lm_evidence "$WORK/vllm-pass1.log"
stop_vllm

echo "============ PASS 2: fresh process (expect HIT from DAOS) ============"
start_vllm "$WORK/vllm-pass2.log" || exit 1
T2=$(ask "list two facts")
echo "  TTFT pass2 (warm/DAOS hit) = ${T2}s"
echo "  DAOS container after pass2:"; kv_stat
echo "  --- LMCache evidence (pass2) ---"; lm_evidence "$WORK/vllm-pass2.log"
stop_vllm

echo "============ RESULT ============"
echo "TTFT cold = ${T1}s      TTFT warm = ${T2}s"
python - "$T1" "$T2" <<'EOF'
import sys
try:
    a,b=float(sys.argv[1]),float(sys.argv[2])
    print(f"speedup = {a/b:.2f}x   (warm is {(1-b/a)*100:.1f}% lower TTFT)")
except Exception as e: print("could not compute:",e)
EOF
fusermount -u $MNT 2>/dev/null || umount $MNT 2>/dev/null
