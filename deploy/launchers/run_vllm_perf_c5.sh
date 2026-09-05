#!/bin/bash
# client-5 performance launcher: kvsup-ucx-lmc (c_ops + UCX 1.20), stockfull DAOS libs, ucx+rc_v servers.
MML=${MML:-32768}
podman rm -f vllm-daos >/dev/null 2>&1; sleep 15
printf 'chunk_size: 256\nlocal_cpu: false\nremote_url: "plugin://daos/attr1/kvlmc5"\nremote_serde: "naive"\nremote_storage_plugins: ["daos"]\nextra_config:\n  remote_storage_plugin.daos.module_path: lmcache_daos.connector\n  remote_storage_plugin.daos.class_name: DaosConnector\nenable_async_loading: True\nmax_local_cpu_size: 100\n' > /root/lmc/perf.yaml
podman run -d --name vllm-daos --net host --security-opt label=disable --device nvidia.com/gpu=all --ipc host \
  --device /dev/infiniband --ulimit memlock=-1:-1 --cap-add=IPC_LOCK \
  -v /root/daoslibs-stock:/daoslib:ro -v /etc/libibverbs.d:/etc/libibverbs.d:ro \
  -v /var/run/daos_agent:/var/run/daos_agent -v /etc/daos:/etc/daos \
  -v /root/lmcache-daos-repo:/lmd -v /root/lmc:/cfg -v /home/hf/hf_cache:/hf \
  -e HF_HOME=/hf -e PYTHONHASHSEED=0 -e PYTHONPATH=/lmd -e LMCACHE_CONFIG_FILE=/cfg/perf.yaml \
  -e VLLM_USE_FLASHINFER_SAMPLER=0 \
  -e LD_LIBRARY_PATH=/usr/local/cuda-12.9/lib64:/usr/local/cuda-12.9/targets/x86_64-linux/lib:${UCXLIB:-/opt/ucx/lib}:/daoslib:/daoslib/mercury:/daoslib/libibverbs \
  localhost/kvsup-ucx-lmc:local \
  numactl --interleave=all \
  vllm serve /hf/hub/models--Qwen--Qwen3-14B/snapshots/40c069824f4251a91eefaf281ebe4c544efd3e18 \
    --served-model-name qwen3 --max-model-len $MML --gpu-memory-utilization 0.90 \
    --enforce-eager --no-enable-prefix-caching \
    --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' --port 8001 >/dev/null 2>&1
