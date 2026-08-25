#!/bin/bash
podman rm -f vllm-daos 2>/dev/null
podman run -d --name vllm-daos --net host --security-opt label=disable --device nvidia.com/gpu=all --ipc host \
  --device /dev/infiniband --ulimit memlock=-1:-1 --cap-add=IPC_LOCK \
  -v /root/daoslib-ucx:/daoslib:ro -v /var/run/daos_agent:/var/run/daos_agent -v /etc/daos:/etc/daos \
  -v /root/lmcache-daos:/lmd -v /root/lmc:/cfg -v /home/hf_cache:/hf \
  -e HF_HOME=/hf -e PYTHONHASHSEED=0 -e PYTHONPATH=/lmd -e DAOS_BG_PROF=1 \
  -e LMCACHE_CONFIG_FILE=/cfg/lmcache-daos.yaml -e VLLM_USE_FLASHINFER_SAMPLER=0 \
  -e LD_LIBRARY_PATH=/usr/local/cuda-12.9/lib64:/usr/local/cuda-12.9/targets/x86_64-linux/lib:/opt/ucx/lib:/daoslib:/daoslib/mercury \
  kvsup-ucx:local \
  vllm serve /hf/hub/models--Qwen--Qwen3-14B/snapshots/40c069824f4251a91eefaf281ebe4c544efd3e18 \
    --served-model-name qwen3 --max-model-len 16384 --gpu-memory-utilization 0.85 \
    --enforce-eager --no-enable-prefix-caching \
    --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' --port 8001
