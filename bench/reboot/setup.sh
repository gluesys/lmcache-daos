#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Usage: setup.sh <step>...   Steps, in order: admin pools code vllm (README.md).
#   admin  the dmg pod (admin.yaml)
#   pools  $POOL (rd_fac 0, self_heal none) and kvec (rd_fac 1, default self_heal), container $CONT in
#          each. TIER_RATIO: the default 6 % leaves a small pool under DAOS's 128 MiB meta blob per
#          target and the create fails; 12 fits 14 GiB on 3 x 4 targets. A DaosPool with
#          spec.profile: kvcache (daos-operator >= v0.1.0-rc.5) computes it instead.
#   code   the model on the GPU node, this checkout's lmcache_daos in /opt/lmcd there
#   vllm   VLLM_MANIFEST (vllm-guard.yaml or vllm-mp.yaml) with the site values filled in
set -e
H=$(dirname "$0"); . "$H/env.sh"
V='NS ADMIN_POD GPU_NODE AGENT_IMAGE ADMIN_IMAGE VLLM_IMAGE VLLM_POD POOL CONT DAOS_SYS GUARD_OCLASS GUARD_CAPACITY_GB MODEL_DIR'
fill() { (export $V; envsubst "$(printf '$%s ' $V)" < "$1"); }
for step in "$@"; do case $step in
admin)
  fill "$H/admin.yaml" | kubectl apply -f -
  kubectl wait -n $NS pod/$ADMIN_POD --for=condition=Ready --timeout=300s
  ;;
pools)
  D pool create -z ${POOL_SIZE:-14GiB} --tier-ratio ${TIER_RATIO:-12} -P rd_fac:0 $POOL | tail -1
  D pool set-prop $POOL self_heal:none | tail -1
  D pool create -z ${POOL_SIZE:-14GiB} --tier-ratio ${TIER_RATIO:-12} -P rd_fac:1 kvec | tail -1
  X "daos cont create $POOL $CONT --properties rd_fac:0 | grep Label; daos cont create kvec $CONT --properties rd_fac:1 | grep Label"
  ;;
code)
  ssh $GPU_SSH "D=$MODEL_DIR/qwen2.5-1.5b-instruct; mkdir -p \$D; cd \$D; B=https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct/resolve/main
    for f in config.json generation_config.json tokenizer.json tokenizer_config.json vocab.json merges.txt model.safetensors; do [ -s \$f ] || curl -sSfL -o \$f \$B/\$f; done; ls \$D | wc -l"
  rsync -a --delete --exclude __pycache__ "$LMCD/lmcache_daos" $GPU_SSH:/opt/lmcd/
  ;;
vllm)
  : "${VLLM_IMAGE:?VLLM_IMAGE: an image with vLLM, LMCache and the DAOS client libraries}"
  fill "$H/${VLLM_MANIFEST:-vllm-guard.yaml}" | kubectl apply -f -
  ;;
*) echo "unknown step $step"; exit 2;;
esac; done
