#!/bin/bash
ARM=$1; podman rm -f vllm-daos >/dev/null 2>&1
Y=/root/lmc/arm.yaml
case $ARM in
  cpu)   printf 'chunk_size: 256\nlocal_cpu: true\nmax_local_cpu_size: 200\nenable_async_loading: True\n' > $Y ;;
  nvme)  printf 'chunk_size: 256\nlocal_cpu: false\nlocal_disk: "file:///kvlocal/"\nmax_local_disk_size: 500\nenable_async_loading: True\n' > $Y ;;
  daos)  printf 'chunk_size: 256\nlocal_cpu: false\nremote_url: "plugin://daos/kvpool2/kv2s16"\nremote_serde: "naive"\nremote_storage_plugins: ["daos"]\nextra_config:\n  remote_storage_plugin.daos.module_path: lmcache_daos.connector\n  remote_storage_plugin.daos.class_name: DaosConnector\nenable_async_loading: True\n' > $Y ;;
esac
KV=""; [ "$ARM" != "recompute" ] && KV="--kv-transfer-config {\"kv_connector\":\"LMCacheConnectorV1\",\"kv_role\":\"kv_both\"}"
podman run -d --name vllm-daos --net host --security-opt label=disable --device nvidia.com/gpu=all --ipc host \
  --device /dev/infiniband --ulimit memlock=-1:-1 --cap-add=IPC_LOCK \
  -v /root/daoslib-ucx:/daoslib:ro -v /etc/libibverbs.d:/etc/libibverbs.d:ro \
  -v /var/run/daos_agent:/var/run/daos_agent -v /etc/daos:/etc/daos \
  -v /root/lmcache-daos:/lmd -v /root/lmc:/cfg -v /home/hf_cache:/hf -v /home/kvlocal:/kvlocal \
  -e HF_HOME=/hf -e PYTHONHASHSEED=0 -e PYTHONPATH=/lmd -e LMCACHE_CONFIG_FILE=/cfg/arm.yaml \
  -e VLLM_USE_FLASHINFER_SAMPLER=0 \
  -e LD_LIBRARY_PATH=/usr/local/cuda-12.9/lib64:/usr/local/cuda-12.9/targets/x86_64-linux/lib:/opt/ucx/lib:/daoslib:/daoslib/mercury \
  kvsup-ucx-lmc:local \
  vllm serve /hf/hub/models--Qwen--Qwen3-14B/snapshots/40c069824f4251a91eefaf281ebe4c544efd3e18 \
    --served-model-name qwen3 --max-model-len 32768 --gpu-memory-utilization 0.85 \
    --enforce-eager --no-enable-prefix-caching $KV --port 8001 >/dev/null 2>&1
