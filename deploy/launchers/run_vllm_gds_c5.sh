#!/bin/bash
# client-5 in-process launcher with the GPU-direct DAOS storage plugin (DaosGdsBackend).
# Needs on the host: /opt/daos-gds-gpu (GPU-direct DAOS client), /opt/ofi-cuda (CUDA libfabric,
# verbs dmabuf patch), /usr/local/cuda-13.3 (libcudart for libfabric's dlopen), libgdrapi.
# Env: MML (max model len), GDS_GB (GPU staging pool), GPU_UTIL, POOL/CONT, WORKERS, ASYNC (enable_async_loading:
# prefetch at lookup time so DAOS->GPU reads of the next request overlap the current prefill), PODMAN_EXTRA.
MML=${MML:-32768}; GDS_GB=${GDS_GB:-6}; GPU_UTIL=${GPU_UTIL:-0.80}
POOL=${POOL:-attr1}; CONT=${CONT:-kvgds_s16}; WORKERS=${WORKERS:-16}; ASYNC=${ASYNC:-False}
podman rm -f vllm-daos >/dev/null 2>&1; sleep 15
cat > /root/lmc/gds.yaml <<Y
chunk_size: 256
local_cpu: false
max_local_cpu_size: 100
storage_plugins: ["daosgds"]
extra_config:
  storage_plugin.daosgds.module_path: lmcache_daos.gds_backend
  storage_plugin.daosgds.class_name: DaosGdsBackend
  daosgds.pool: $POOL
  daosgds.container: $CONT
  daosgds.gpu_buffer_gb: $GDS_GB
  daosgds.io_workers: $WORKERS
enable_async_loading: $ASYNC
Y
read -r -d '' INNER <<'EOS'
mkdir -p /tmp/links && cp -f /cuda13/libcudart.so.13 /tmp/links/libcudart.so
CUDA1=$(ldconfig -p | awk '/libcuda.so.1 /{print $NF; exit}'); [ -n "$CUDA1" ] && ln -sf "$CUDA1" /tmp/links/libcuda.so
G=/opt/daos-gds-gpu; PRE=$(ls -d $G/prereq/release/*/lib64 | grep -v '/ofi/' | tr '\n' ':')
export LD_LIBRARY_PATH=/tmp/links:/opt/gdr:/opt/ofi-cuda/lib64:$G/lib64:${PRE}/daoslib/libibverbs:/daoslib:/usr/local/cuda-12.9/lib64:/usr/local/cuda-12.9/targets/x86_64-linux/lib
export LMCACHE_DAOS_LIBDIR=$G/lib64 D_MEM_DEVICE=1 D_GPU_DIRECT=1 FI_LOG_LEVEL=error
exec numactl --interleave=all vllm serve "$@"
EOS
podman run -d --name vllm-daos --net host --security-opt label=disable --device nvidia.com/gpu=all --ipc host \
  --device /dev/infiniband --ulimit memlock=-1:-1 --cap-add=IPC_LOCK --ulimit nofile=65536:65536 \
  -v /root/daoslibs-stock:/daoslib:ro -v /etc/libibverbs.d:/etc/libibverbs.d:ro \
  -v /opt/daos-gds-gpu:/opt/daos-gds-gpu:ro -v /opt/ofi-cuda:/opt/ofi-cuda:ro \
  -v /usr/local/cuda-13.3/lib64:/cuda13:ro -v /usr/lib64/libgdrapi.so.2.6:/opt/gdr/libgdrapi.so.2:ro \
  -v /var/run/daos_agent:/var/run/daos_agent -v /etc/daos:/etc/daos \
  -v /root/lmcache-daos-repo:/lmd -v /root/lmc:/cfg -v /home/hf/hf_cache:/hf \
  -e HF_HOME=/hf -e PYTHONHASHSEED=0 -e PYTHONPATH=/lmd -e LMCACHE_CONFIG_FILE=/cfg/gds.yaml \
  -e VLLM_USE_FLASHINFER_SAMPLER=0 -e DAOS_GDS_MULTI_PREFETCH=${MULTI:-1} ${PODMAN_EXTRA:-} \
  localhost/kvsup-ucx-lmc:local \
  bash -c "$INNER" -- /hf/hub/models--Qwen--Qwen3-14B/snapshots/40c069824f4251a91eefaf281ebe4c544efd3e18 \
    --served-model-name qwen3 --max-model-len $MML --gpu-memory-utilization $GPU_UTIL \
    --enforce-eager --no-enable-prefix-caching \
    --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' --port 8001 >/dev/null 2>&1
