#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Run kv_correctness_gate.sh against BOTH layouts, same model, same prompts.
#
# vLLM's own prefix cache is turned OFF here. With it on, pass B is served from
# GPU without ever asking LMCache -- the log showed a 45% vLLM prefix hit rate
# and not one LMCache retrieve -- and the gate correctly reported INCONCLUSIVE
# rather than a pass. The gate is about whether a RESTORED KV matches a computed
# one, so the tier under test has to be the one answering.
#
# doc/RAW-API-PLAN.md step 4. The gate's own advice is to establish that the
# known-good configuration passes before believing a new one, so this runs the
# DFS container first and the dkey/akey container second rather than reporting
# the new path on its own. A green run on the object API means nothing if the
# harness was broken that day.
#
#   bash tests/raw_gate_ab.sh [pool] [model]
#
# Creates two containers, starts vLLM once per layout, and leaves nothing
# running. The non-POSIX one is created with rd_fac:0 -- the pool default of 1
# makes the object path repeat DER_HG forever (doc/FAILURE-MODES.md).
set -u
POOL=${POOL:-${1:-kvpool}}
# A local path, not a hub id. The gated meta-llama repos return 403 without a
# token even when a partial copy is cached, and the failure arrives as "engine
# core initialization failed" -- which reads like a backend problem and is not.
MODEL=${MODEL:-${2:-/mnt/nvme1/models/TinyLlama-1.1B-Chat-v1.0}}
PORT=${PORT:-8011}
N=${N:-6}
MAXTOK=${MAXTOK:-24}
GPUUTIL=${GPUUTIL:-0.40}
# 2048 because TinyLlama's max_position_embeddings is 2048; a larger value is
# refused at startup, not clamped. Override for a longer-context model.
MAXLEN=${MAXLEN:-2048}
# ~65 tokens per repeat: 25 is ~1600 tokens, which leaves headroom under a 2048
# window and still spans six 256-token chunks.
PARA_REPEAT=${PARA_REPEAT:-25}
REPO=${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
WORK=${WORK:-/tmp/raw_gate}
DAOS_BIN=${DAOS_BIN:-}
mkdir -p "$WORK"
export PATH=${DAOS_BIN:+$DAOS_BIN:}$PATH
export PYTHONPATH=$REPO${PYTHONPATH:+:$PYTHONPATH}
export HF_HOME=${HF_HOME:-/mnt/nvme1/hf_cache}
export VLLM_USE_FLASHINFER_SAMPLER=0
# Fail fast and locally rather than reaching for the hub mid-startup.
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}

stop_vllm() {
  pkill -f "vllm serve" 2>/dev/null
  for _ in $(seq 1 40); do pgrep -f "vllm serve" >/dev/null || break; sleep 2; done
}
trap stop_vllm EXIT

run_one() {   # run_one <container> <label>
  local cont=$1 label=$2 log=$WORK/vllm-$1.log
  echo
  printf '\033[1m### %s  (pool=%s cont=%s)\033[0m\n' "$label" "$POOL" "$cont"

  cat > "$WORK/lmcache-$cont.yaml" <<YAML
chunk_size: 256
remote_url: "plugin://daos/${POOL}/${cont}"
remote_serde: "naive"
remote_storage_plugins: ["daos"]
extra_config:
  remote_storage_plugin.daos.module_path: lmcache_daos.connector
  remote_storage_plugin.daos.class_name: DaosConnector
YAML
  export LMCACHE_CONFIG_FILE=$WORK/lmcache-$cont.yaml

  stop_vllm; rm -f "$log"
  nohup vllm serve "$MODEL" --host 127.0.0.1 --port $PORT \
    --tensor-parallel-size 1 --gpu-memory-utilization $GPUUTIL \
    --max-model-len $MAXLEN --enforce-eager --no-enable-prefix-caching \
    --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
    > "$log" 2>&1 &
  for i in $(seq 1 120); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    pgrep -f "vllm serve" >/dev/null || { echo "  vllm DIED"; tail -20 "$log"; return 1; }
    sleep 5
  done
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null || { echo "  vllm never came up"; return 1; }

  # Which backend did the connector actually pick? Printing it is the whole
  # point of an A/B: a run that silently used DFS for both arms would pass
  # twice and prove half as much.
  grep -m1 -oE "using the object API \(dkey/akey\)" "$log" \
    && echo "  -> backend: object API" || echo "  -> backend: DFS"

  # The gate posts to /v1/completions, so the model field must be exactly what
  # vLLM serves -- which for a local path is the path. And the prompt has to fit
  # the context window with room for max_tokens.
  LOG_CMD="cat $log" GATE_MODEL="$MODEL" PARA_REPEAT="$PARA_REPEAT" \
    bash "$REPO/tests/kv_correctness_gate.sh" \
      "http://127.0.0.1:$PORT/v1/completions" "$N" "$MAXTOK"
  local rc=$?
  stop_vllm
  return $rc
}

echo "model=$MODEL prompts=$N max_tokens=$MAXTOK  work=$WORK"
daos cont create "$POOL" kvgate_posix --type POSIX --properties=rd_fac:0 >/dev/null 2>&1
daos cont create "$POOL" kvgate_raw --properties=rd_fac:0 >/dev/null 2>&1

rc_posix=0; rc_raw=0
run_one kvgate_posix "A. DFS (known-good control)" || rc_posix=$?
run_one kvgate_raw   "B. object API (dkey/akey)"   || rc_raw=$?

printf '\n\033[1m=== DFS %s | object API %s ===\033[0m\n' \
  "$([ $rc_posix -eq 0 ] && echo PASS || echo FAIL)" \
  "$([ $rc_raw -eq 0 ] && echo PASS || echo FAIL)"
exit $(( rc_posix != 0 || rc_raw != 0 ))
