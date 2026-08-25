#!/bin/bash
podman rm -f vllm-daos 2>/dev/null
podman run -d --name vllm-daos --net host --security-opt label=disable --device nvidia.com/gpu=all --ipc host \
  --device /dev/infiniband --ulimit memlock=-1:-1 --cap-add=IPC_LOCK \
  -v /root/daoslibs:/daoslib:ro -v /etc/libibverbs.d:/etc/libibverbs.d:ro \
  -v /var/run/daos_agent:/var/run/daos_agent -v /etc/daos:/etc/daos \
  -v /root/lmcache-daos:/lmd -v /root/lmc:/cfg -v /root/hf_cache:/hf \
  -e HF_HOME=/hf -e PYTHONHASHSEED=0 -e PYTHONPATH=/lmd \
  -e LMCACHE_CONFIG_FILE=/cfg/lmcache-daos.yaml -e VLLM_USE_FLASHINFER_SAMPLER=0 -e FI_MR_CACHE_MAX_COUNT=0 -e FI_MR_CACHE_MONITOR=disabled \
  -e LD_LIBRARY_PATH=/usr/local/cuda-12.9/lib64:/usr/local/cuda-12.9/targets/x86_64-linux/lib:/daoslib:/daoslib/mercury:/daoslib/libibverbs \
  kvsup:052 \
  vllm serve /hf/hub/models--Qwen--Qwen3-1.7B/snapshots/70d244cc86ccca08cf5af4e1e306ecf908b1ad5e \
    --served-model-name qwen3 --max-model-len 8192 --gpu-memory-utilization 0.85 \
    --enforce-eager --no-enable-prefix-caching \
    --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' --port 8001
