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
#
# /daoslib MUST come from a client bundle built from the SAME DAOS as the
# servers run. This mounted /root/daoslibs, a bundle built from a different
# DAOS than the 2.9.100 servers, and that pairing silently corrupted data:
# concurrent multi-chunk reads returned one 4 MiB region wrong while sizes and
# return codes stayed normal, so vLLM restored wrong KV and generation just
# drifted. Identical test, only the library path changed -- old bundle failed
# repeatedly, matching client passed 80/80. dmg rejects a mismatched control
# plane outright; the data plane does not, so nothing warns you.
#
# You cannot tell the builds apart by filename: DAOS names the library
# libdaos.so.2.8.0 regardless of version. Compare content -- ~2.1 MB for the
# stock 2.8 RPM build, ~8.9 MB for the 2.9.100 source build. Rebuild the bundle
# with deploy/mk_daoslibs_bundle.sh whenever the servers are rebuilt, and prove
# it with tests/test_rawio_integrity.py before trusting it.
#
# NA_UCX_EXTRA_TLS= is REQUIRED, not tidiness. This client carries
# gpudirect/patches/mercury-0001-keep-cuda-memtype-tls.patch, which appends
# cuda_copy,cuda_ipc to UCX's TLS list so that dfs_read_gpu() can register GPU
# memory. That patch defaults ON, and once CUDA is loadable -- which it always
# is in this container, CUDA leads LD_LIBRARY_PATH -- UCX brings up those
# memory-type components and then corrupts ORDINARY HOST-MEMORY bulk transfers:
# one 4 MiB region per transfer, only with 4 or more concurrent readers, sizes
# and return codes all normal. Setting this empty disables the appended TLS and
# the same test passes 80/80. Measured on client-6, everything else identical:
#   CUDA on the path, patch default : FAIL
#   CUDA on the path, this set empty: PASS 80/80
# Do not "clean this up", and do not rely on library ordering instead -- putting
# /daoslib ahead of CUDA also passes, but only because UCX then fails to find
# CUDA at all, which is accidental rather than intended.
podman rm -f vllm-daos 2>/dev/null
podman run -d --name vllm-daos --net host --security-opt label=disable --device nvidia.com/gpu=all --ipc host \
  --device /dev/infiniband --ulimit memlock=-1:-1 --cap-add=IPC_LOCK \
  -v /root/daoslibs29:/daoslib:ro -v /etc/libibverbs.d:/etc/libibverbs.d:ro \
  -v /var/run/daos_agent:/var/run/daos_agent -v /etc/daos:/etc/daos \
  -v /root/lmcache-daos:/lmd -v /root/lmc:/cfg -v /root/hf_cache:/hf \
  -e HF_HOME=/hf -e PYTHONHASHSEED=0 -e PYTHONPATH=/lmd \
  -e LMCACHE_CONFIG_FILE=/cfg/lmcache-daos.yaml -e VLLM_USE_FLASHINFER_SAMPLER=0 -e FI_MR_CACHE_MAX_COUNT=0 -e FI_MR_CACHE_MONITOR=disabled \
  -e NA_UCX_EXTRA_TLS= \
  -e LD_LIBRARY_PATH=/usr/local/cuda-12.9/lib64:/usr/local/cuda-12.9/targets/x86_64-linux/lib:/daoslib:/daoslib/mercury:/daoslib/libibverbs \
  kvsup:052 \
  numactl --interleave=all \
  vllm serve /hf/hub/models--Qwen--Qwen3-1.7B/snapshots/70d244cc86ccca08cf5af4e1e306ecf908b1ad5e \
    --served-model-name qwen3 --max-model-len 8192 --gpu-memory-utilization 0.85 \
    --enforce-eager --no-enable-prefix-caching \
    --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' --port 8001
