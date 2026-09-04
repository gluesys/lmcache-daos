#!/bin/bash
# client-5 launcher for LMCache MP mode: one container, two processes.
#   1. lmcache_daos.mp.server  -- ZMQ cache server owning L1 (pinned CPU) and
#      the DAOS L2 adapter (type "daos", registered by our entry point)
#   2. vllm serve              -- LMCacheMPConnector talks to it over ZMQ
# LMCACHE_CONFIG_FILE is not used in MP mode; the server flags are the config.
# DaosMPConnector (lmcache_daos/mp/vllm_connector.py) re-exports LMCache's own MP
# connector under an unregistered name: vLLM 0.18 resolves 'LMCacheMPConnector'
# to its bundled, stale copy before honouring kv_connector_module_path.
MML=${MML:-32768}
L1_GB=${L1_GB:-100}
MP_PORT=${MP_PORT:-5555}
POOL=${POOL:-attr1}; CONT=${CONT:-kvlmc5}; ROOT=${ROOT:-/mp}
WORKERS=${WORKERS:-8}; GPU_WORKERS=${GPU_WORKERS:-4}; CPU_WORKERS=${CPU_WORKERS:-8}; STATUS_S=${STATUS_S:-0}
MODEL=${MODEL:-/hf/hub/models--Qwen--Qwen3-14B/snapshots/40c069824f4251a91eefaf281ebe4c544efd3e18}
# YaRN for contexts beyond the model's native 40960 (same as run_arm_yarn.sh):
#   MML=66560 FACTOR=1.625 (64K)   MML=131072 FACTOR=3.2 (127K)
FACTOR=${FACTOR:-}
YARN=""
[ -n "$FACTOR" ] && YARN="--hf-overrides '{\"max_position_embeddings\":$MML,\"rope_scaling\":{\"rope_type\":\"yarn\",\"factor\":$FACTOR,\"original_max_position_embeddings\":40960}}'"

podman rm -f vllm-daos >/dev/null 2>&1; sleep 15

read -r -d '' INNER <<EOF
set -m
python3 -m lmcache_daos.mp.server --host localhost --port $MP_PORT --chunk-size 256 \
  --l1-size-gb $L1_GB --eviction-policy LRU \
  --max-gpu-workers $GPU_WORKERS --max-cpu-workers $CPU_WORKERS \
  --l2-adapter '{"type":"daos","pool":"$POOL","container":"$CONT","root":"$ROOT","workers":$WORKERS,"status_interval_s":$STATUS_S}' \
  --disable-observability > >(sed -u 's/^/[mp-server] /') 2>&1 &
SRV=\$!
for i in \$(seq 1 180); do
  (echo > /dev/tcp/127.0.0.1/$MP_PORT) 2>/dev/null && break
  kill -0 \$SRV 2>/dev/null || { echo "[launcher] mp server exited before listening"; exit 1; }
  sleep 1
done
echo "[launcher] mp server port open after \$i s"
exec numactl --interleave=all vllm serve $MODEL \
  --served-model-name qwen3 --max-model-len $MML --gpu-memory-utilization 0.90 $YARN \
  --enforce-eager --no-enable-prefix-caching --port 8001 \
  --kv-transfer-config '{"kv_connector":"DaosMPConnector","kv_connector_module_path":"lmcache_daos.mp.vllm_connector","kv_role":"kv_both","kv_connector_extra_config":{"lmcache.mp.host":"tcp://localhost","lmcache.mp.port":$MP_PORT}}'
EOF

podman run -d --name vllm-daos --net host --security-opt label=disable --device nvidia.com/gpu=all --ipc host \
  --device /dev/infiniband --ulimit memlock=-1:-1 --cap-add=IPC_LOCK \
  -v /root/daoslibs-stock:/daoslib:ro -v /etc/libibverbs.d:/etc/libibverbs.d:ro \
  -v /var/run/daos_agent:/var/run/daos_agent -v /etc/daos:/etc/daos \
  -v /root/lmcache-daos-repo:/lmd -v /home/hf/hf_cache:/hf \
  -e HF_HOME=/hf -e PYTHONHASHSEED=0 -e PYTHONPATH=/lmd \
  -e VLLM_USE_FLASHINFER_SAMPLER=0 -e D_LOG_MASK=${D_LOG_MASK:-WARN} -e D_LOG_FILE=${D_LOG_FILE:-/tmp/daos_client.log} \
  -e LD_LIBRARY_PATH=/usr/local/cuda-12.9/lib64:/usr/local/cuda-12.9/targets/x86_64-linux/lib:${UCXLIB:-/opt/ucx/lib}:/daoslib:/daoslib/mercury:/daoslib/libibverbs \
  localhost/kvsup-ucx-lmc:local \
  bash -c "$INNER" >/dev/null 2>&1
