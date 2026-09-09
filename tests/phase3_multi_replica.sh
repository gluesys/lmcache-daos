#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
# Phase 3 (cont.): two independent vLLM replicas sharing one DAOS L2 container.
#
# SCOPE -- read this before quoting the numbers. Both replicas run on the SAME
# host and the SAME GPU, so this is cross-*replica* / cross-*process* reuse, not
# the cross-*node* case the design doc targets: there is no network hop and no
# per-node NIC/CPU contention here. What it does establish is the functional
# core -- replica B, whose local CPU tier never saw the prefix, serves it from
# the shared DAOS container that replica A wrote.
#
#   POOL=nvme_pool CONT=lmcache_nvme bash tests/phase3_multi_replica.sh
set -u
REPO_DIR=${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
VENV=${VENV:-$REPO_DIR/../venv-lmcache}
WORK=${WORK:-${TMPDIR:-/tmp}/lmcache-daos-multi}
mkdir -p "$WORK"
cd "$REPO_DIR"
# shellcheck disable=SC1091
[ -f "$VENV/bin/activate" ] && source "$VENV/bin/activate"
export HF_HOME=${HF_HOME:-$WORK/hf}
export VLLM_USE_FLASHINFER_SAMPLER=0
export PYTHONHASHSEED=0          # both replicas must hash keys identically
export CUDA_HOME=${CUDA_HOME:-/usr/local/cuda}

MODEL=${MODEL:-Qwen/Qwen3-1.7B}
POOL=${POOL:-nvme_pool}
CONT=${CONT:-lmcache_nvme}
MNT=${MNT:-/mnt/lmcache_kv_multi}
PROMPT_TOKENS=${PROMPT_TOKENS:-4096}
MAXLEN=${MAXLEN:-8192}
# A2 = 15 GiB total; 1.7B bf16 weights ~3.4 GiB per replica. 0.42 each leaves
# both engines ~3 GiB of KV and ~2 GiB of headroom on the card.
GPUUTIL=${GPUUTIL:-0.42}
PURGE=${PURGE:-1}
PORT_A=${PORT_A:-8000}
PORT_B=${PORT_B:-8001}

LMCACHE_CONFIG_FILE=$WORK/lmcache-run.yaml
cat > "$LMCACHE_CONFIG_FILE" <<EOF
chunk_size: 256
remote_url: "plugin://daos/${POOL}/${CONT}"
remote_serde: "naive"
remote_storage_plugins: ["daos"]
extra_config:
  remote_storage_plugin.daos.module_path: lmcache_daos.connector
  remote_storage_plugin.daos.class_name: DaosConnector
EOF
export LMCACHE_CONFIG_FILE
echo "### pool=$POOL cont=$CONT model=$MODEL gpuutil=$GPUUTIL x2"

mount_dfuse() {
  mkdir -p "$MNT"
  mountpoint -q "$MNT" || dfuse --pool "$POOL" --container "$CONT" \
      --mountpoint "$MNT" --disable-caching >/dev/null 2>&1
  sleep 2
}
kv_stat() {
  mountpoint -q "$MNT" || return
  echo "    files=$(find "$MNT" -type f 2>/dev/null | wc -l) bytes=$(du -sb "$MNT" 2>/dev/null | cut -f1)"
}
reap_gpu() {
  for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do
    kill -9 "$p" 2>/dev/null
  done
  for i in $(seq 1 20); do
    [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null)" ] && break
    sleep 2
  done
  sleep 3
}
stop_all() {
  pkill -f "vllm serve" 2>/dev/null
  for i in $(seq 1 30); do pgrep -f "vllm serve" >/dev/null || break; sleep 2; done
  reap_gpu
  echo "    gpu: $(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader)"
}
trap stop_all EXIT

start_replica() {  # $1=port $2=log
  local port=$1 log=$2
  rm -f "$log"
  nohup vllm serve "$MODEL" \
    --host 127.0.0.1 --port "$port" \
    --tensor-parallel-size 1 \
    --gpu-memory-utilization $GPUUTIL \
    --max-model-len $MAXLEN \
    --enforce-eager \
    --kv-transfer-config \
      '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
    > "$log" 2>&1 &
  for i in $(seq 1 110); do
    curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1 && { echo "  replica :$port up (~$((i*8))s)"; return 0; }
    sleep 8
  done
  echo "  replica :$port FAILED"; tail -25 "$log"; return 1
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

ask() {  # $1=port $2=question -> TTFT seconds
  python - "$1" "$PROMPT" "$2" "$MODEL" <<'EOF'
import json,sys,time,urllib.request
port,prompt,q,model=sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]
body=json.dumps({"model":model,"prompt":prompt+"\n\nQuestion: "+q+"\nAnswer:",
  "max_tokens":16,"temperature":0,"stream":True}).encode()
req=urllib.request.Request(f"http://127.0.0.1:{port}/v1/completions",body,
  {"Content-Type":"application/json"})
t0=time.time()
with urllib.request.urlopen(req) as r:
    for line in r:
        if line.startswith(b"data: ") and b"[DONE]" not in line:
            print(f"{time.time()-t0:.3f}"); break
EOF
}

hits() { grep -oE "LMCache hit tokens: [0-9]+, need to load: [0-9]+" "$1" | tail -1; }

mount_dfuse
stop_all
if [ "$PURGE" = "1" ]; then
  echo "purging $POOL/$CONT so replica A is a genuine miss"
  find "$MNT" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null
fi
echo "container before:"; kv_stat

echo "============ replica A: writer ============"
start_replica "$PORT_A" "$WORK/A.log" || exit 1
TA=$(ask "$PORT_A" "summarize in one line")
echo "  A TTFT (cold) = ${TA}s"
sleep 10
echo "  container after A:"; kv_stat
echo "  A: $(hits "$WORK/A.log")"

echo "============ replica B: reader (own L1 never saw this prefix) ============"
start_replica "$PORT_B" "$WORK/B.log" || exit 1
TB=$(ask "$PORT_B" "list two facts")
echo "  B TTFT (shared-L2 hit) = ${TB}s"
sleep 6
echo "  container after B:"; kv_stat
echo "  B: $(hits "$WORK/B.log")"

echo "============ concurrent shared-L2 reads ============"
# Both replicas must have COLD local tiers for this to measure DAOS at all.
# Without the restart both engines already hold the KV locally and report
# "need to load: 0" -- that is a local-cache hit, not a shared-L2 read.
stop_all
start_replica "$PORT_A" "$WORK/A2.log" || exit 1
start_replica "$PORT_B" "$WORK/B2.log" || exit 1
# `VAR=$(cmd) &` assigns inside a subshell and the value is lost, so the two
# in-flight requests write their TTFT to files instead.
( ask "$PORT_A" "name one component" > "$WORK/ttft_a" ) &
pa=$!
( ask "$PORT_B" "give one detail"    > "$WORK/ttft_b" ) &
pb=$!
wait $pa $pb 2>/dev/null
C1=$(cat "$WORK/ttft_a" 2>/dev/null)
C2=$(cat "$WORK/ttft_b" 2>/dev/null)
echo "  A concurrent TTFT = ${C1:-NA}s   (simultaneous with B)"
echo "  B concurrent TTFT = ${C2:-NA}s   (simultaneous with A)"
echo "  container after concurrent:"; kv_stat
echo "  A: $(hits "$WORK/A2.log")   <- need-to-load>0 means it really came from DAOS"
echo "  B: $(hits "$WORK/B2.log")   <- need-to-load>0 means it really came from DAOS"
for L in "$WORK/A2.log" "$WORK/B2.log"; do
  echo "  $(basename "$L"): $(grep -oE "Retrieved [0-9]+ out of [0-9]+ required tokens[^[]*" "$L" | tail -1)"
done

echo "============ RESULT ============"
echo "A cold (miss->store)             = ${TA}s"
echo "B shared-L2 hit (solo)           = ${TB}s"
echo "A/B shared-L2 hit (simultaneous) = ${C1:-NA}s / ${C2:-NA}s"
python - "$TA" "$TB" <<'EOF'
import sys
try:
    a,b=float(sys.argv[1]),float(sys.argv[2])
    print(f"cross-replica speedup = {a/b:.2f}x  (B is {(1-b/a)*100:.1f}% lower TTFT than A's cold path)")
except Exception as e: print("could not compute:",e)
EOF
fusermount -u "$MNT" 2>/dev/null || umount "$MNT" 2>/dev/null
