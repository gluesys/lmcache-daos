#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
ARM=$1; MML=${MML:-66560}; FACTOR=${FACTOR:-2.0}
podman rm -f vllm-daos >/dev/null 2>&1; sleep 12
Y=/root/lmc/arm.yaml
case $ARM in
  cpu)   printf 'chunk_size: 256\nlocal_cpu: true\nmax_local_cpu_size: 300\nenable_async_loading: True\n' > $Y ;;
  nvme)  printf 'chunk_size: 256\nlocal_cpu: false\nlocal_disk: "file:///kvlocal/"\nmax_local_disk_size: 800\nmax_local_cpu_size: 100\nenable_async_loading: True\n' > $Y ;;
  daos)  printf 'chunk_size: 256\nlocal_cpu: false\nremote_url: "plugin://daos/gdspool/kvlmc"\nremote_serde: "naive"\nremote_storage_plugins: ["daos"]\nextra_config:\n  remote_storage_plugin.daos.module_path: lmcache_daos.connector\n  remote_storage_plugin.daos.class_name: DaosConnector\nenable_async_loading: True\nmax_local_cpu_size: 100\n' > $Y ;;
esac
ARGS=(--served-model-name qwen3 --max-model-len $MML
      --hf-overrides "{\"max_position_embeddings\":$MML,\"rope_scaling\":{\"rope_type\":\"yarn\",\"factor\":$FACTOR,\"original_max_position_embeddings\":40960}}"
      --gpu-memory-utilization 0.90 --enforce-eager --no-enable-prefix-caching --port 8001)
[ "$ARM" != "recompute" ] && ARGS+=(--kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}')
podman run -d --name vllm-daos --net host --security-opt label=disable --device nvidia.com/gpu=all --ipc host \
  --device /dev/infiniband --ulimit memlock=-1:-1 --cap-add=IPC_LOCK \
  -v /root/daoslib-ucx:/daoslib:ro -v /etc/libibverbs.d:/etc/libibverbs.d:ro \
  -v /var/run/daos_agent:/var/run/daos_agent -v /etc/daos:/etc/daos \
  -v /root/lmcache-daos:/lmd -v /root/lmc:/cfg -v /home/hf_cache:/hf -v /home/kvlocal:/kvlocal \
  -e HF_HOME=/hf -e PYTHONHASHSEED=0 -e PYTHONPATH=/lmd -e LMCACHE_CONFIG_FILE=/cfg/arm.yaml \
  -e VLLM_USE_FLASHINFER_SAMPLER=0 \
  -e LD_LIBRARY_PATH=/usr/local/cuda-12.9/lib64:/usr/local/cuda-12.9/targets/x86_64-linux/lib:/opt/ucx/lib:/daoslib:/daoslib/mercury \
  kvsup-ucx-lmc:local \
  vllm serve /hf/hub/models--Qwen--Qwen3-14B/snapshots/40c069824f4251a91eefaf281ebe4c544efd3e18 "${ARGS[@]}" >/dev/null 2>&1
