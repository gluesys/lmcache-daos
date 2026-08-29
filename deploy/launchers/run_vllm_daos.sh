#!/bin/bash
#
# numactl --interleave=all is not a tuning guess. Measured with per-socket
# memory-controller counters, the KV staging path put 81% of its host DRAM
# traffic on socket 1 while both the NIC and the GPU sit on socket 0 -- every
# staged byte crossed UPI, and the ceiling that applied was one socket's
# 220.6 GB/s instead of the node's 401.7. Interleaving evens it to 52/48 and
# bandwidth does not drop; it rose slightly (34.77 -> 35.46 GB/s), because DRAM
# is not the bottleneck in this range. See gpudirect/README.md.
#
# Do not replace it with --cpuset-mems: that restricts which nodes may be used
# but leaves the default local-allocation policy, so it does not interleave.
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
  numactl --interleave=all \
  vllm serve /hf/hub/models--Qwen--Qwen3-1.7B/snapshots/70d244cc86ccca08cf5af4e1e306ecf908b1ad5e \
    --served-model-name qwen3 --max-model-len 8192 --gpu-memory-utilization 0.85 \
    --enforce-eager --no-enable-prefix-caching \
    --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' --port 8001
