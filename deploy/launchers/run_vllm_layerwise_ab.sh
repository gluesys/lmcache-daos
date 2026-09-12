#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# A/B launcher for the layerwise measurement (doc/LAYERWISE-MEASUREMENT.md).
# Derived from run_vllm_perf_c5.sh; the comments there about /daoslib matching
# the server build and about NA_UCX_EXTRA_TLS apply here unchanged.
#
#   MODE=base|layerwise (argv 1)   CHUNK=<tokens>   DAOS_WORKERS=<n>
#
# base      : enable_async_loading True, one object per chunk (all layers)
# layerwise : use_layerwise True, one object per (chunk, layer) -- 40x more
#             objects on Qwen3-14B. async loading is off because upstream
#             marks the two mutually exclusive.
# usage: launch.sh <base|layerwise>
set -u
MODE=${1:-base}
MML=${MML:-32768}
CFG=/root/lmc/exp_${MODE}_${CHUNK:-256}.yaml
{
  echo "chunk_size: ${CHUNK:-256}"
  echo 'local_cpu: false'
  echo 'remote_url: "plugin://daos/attr1/kvlmc5"'
  echo 'remote_serde: "naive"'
  echo 'remote_storage_plugins: ["daos"]'
  echo 'extra_config:'
  echo '  remote_storage_plugin.daos.module_path: lmcache_daos.connector'
  echo '  remote_storage_plugin.daos.class_name: DaosConnector'
  echo 'max_local_cpu_size: 100'
  if [ "$MODE" = layerwise ]; then
    echo 'enable_async_loading: False'
    echo 'use_layerwise: True'
  else
    echo 'enable_async_loading: True'
  fi
} > "$CFG"
echo "=== config ($MODE) ==="; cat "$CFG"

podman rm -f vllm-daos >/dev/null 2>&1; sleep 12
podman run -d --name vllm-daos --net host --security-opt label=disable --device nvidia.com/gpu=all --ipc host \
  --device /dev/infiniband --ulimit memlock=-1:-1 --cap-add=IPC_LOCK \
  -v /root/daoslibs-stock:/daoslib:ro -v /etc/libibverbs.d:/etc/libibverbs.d:ro \
  -v /var/run/daos_agent:/var/run/daos_agent -v /etc/daos:/etc/daos \
  -v /root/lmcache-daos-repo:/lmd -v /root/lmc:/cfg -v /home/hf/hf_cache:/hf \
  -e HF_HOME=/hf -e PYTHONHASHSEED=0 -e PYTHONPATH=/lmd -e LMCACHE_CONFIG_FILE=/cfg/exp_${MODE}_${CHUNK:-256}.yaml \
  -e VLLM_USE_FLASHINFER_SAMPLER=0 -e DAOS_WORKERS=${DAOS_WORKERS:-16} \
  -e LD_LIBRARY_PATH=/usr/local/cuda-12.9/lib64:/usr/local/cuda-12.9/targets/x86_64-linux/lib:/daoslib:/daoslib/mercury:/daoslib/libibverbs \
  localhost/kvsup-ucx-lmc:local \
  numactl --interleave=all \
  vllm serve /hf/hub/models--Qwen--Qwen3-14B/snapshots/40c069824f4251a91eefaf281ebe4c544efd3e18 \
    --served-model-name qwen3 --max-model-len $MML --gpu-memory-utilization 0.90 \
    --enforce-eager --no-enable-prefix-caching \
    --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' --port 8001 >/dev/null 2>&1

echo "=== waiting for /health ==="
for i in $(seq 1 100); do
  if curl -sf --max-time 3 http://localhost:8001/health >/dev/null 2>&1; then
    echo "READY after ${i}0s"; exit 0
  fi
  if ! podman ps --format '{{.Names}}' | grep -q '^vllm-daos$'; then
    echo "CONTAINER DIED"; podman logs --tail 40 vllm-daos 2>&1 | tail -40; exit 1
  fi
  sleep 10
done
echo "TIMEOUT"; podman logs --tail 40 vllm-daos 2>&1 | tail -40; exit 1
