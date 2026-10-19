# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
# shellcheck shell=bash
#
# Site settings and helpers for the reboot test bed (README.md). Sourced by the other scripts;
# set the values below in the environment or in a local file named by $REBOOT_ENV.
[ -n "$REBOOT_ENV" ] && . "$REBOOT_ENV"
: "${GPU_NODE:?GPU_NODE: Kubernetes node name of the GPU node that runs vLLM}"
NS=${NS:-daos-system}                    # namespace of the DaosSystem (daos-operator)
ADMIN_POD=${ADMIN_POD:-kvt-admin}        # dmg pod (admin.yaml)
GPU_SSH=${GPU_SSH:-root@$GPU_NODE}       # ssh target of the GPU node (model, /opt/lmcd, pod logs)
VLLM_URL=${VLLM_URL:-http://$GPU_NODE:8100}
VLLM_POD=${VLLM_POD:-vllm-guard}         # vllm-mp for vllm-mp.yaml
POOL=${POOL:-kvt}; CONT=${CONT:-vllm}
GUARD_OCLASS=${GUARD_OCLASS:-S1}           # EC_2P1G1 with POOL=kvec
GUARD_CAPACITY_GB=${GUARD_CAPACITY_GB:-}  # rank guard eviction bound; empty = none
DAOS_SYS=${DAOS_SYS:-daos_server}
AGENT_IMAGE=${AGENT_IMAGE:-ghcr.io/gluesys/daos-agent:2.8.0-20260914}
ADMIN_IMAGE=${ADMIN_IMAGE:-ghcr.io/gluesys/daos-admin:2.8.0-20260914}
MODEL_DIR=${MODEL_DIR:-/mnt/hf_cache}    # on the GPU node; the model goes in $MODEL_DIR/qwen2.5-1.5b-instruct
LMCD=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)   # this checkout; copied to /opt/lmcd

# dmg in the admin pod
D() { timeout 60 kubectl exec -n $NS $ADMIN_POD -- dmg -o /etc/daos/daos_control.yml "$@" 2>&1; }
# the daos CLI in the CSI node pod on the GPU node (it has an agent)
X() {
  local p=$(kubectl get pod -n $NS -o wide --no-headers | awk -v n=$GPU_NODE '/^daos-csi-node-/ && $7==n {print $1}')
  timeout ${XT:-60} kubectl exec -n $NS $p -c daos-csi -- sh -c "$1" 2>&1
}
# power a storage node off / on: POWER_OFF and POWER_ON are commands with %s for the node id,
# e.g. POWER_OFF='qm stop %s --timeout 120' POWER_ON='qm start %s' (Proxmox), or ipmitool calls
power() { local c; [ $1 = off ] && c=$POWER_OFF || c=$POWER_ON
  : "${c:?POWER_OFF / POWER_ON not set}"; sh -c "$(printf "$c" "$2")"; }
# wait until rank $1 is Joined again (at most $2 s, default 600)
wait_joined() { local s=$(date +%s) q
  while [ $(( $(date +%s) - s )) -lt ${2:-600} ]; do
    q=$(D system query -r $1 | grep -E "^ *$1 "); echo "$(date +%T) rank$1: $q"
    echo "$q" | grep -qi joined && return 0; sleep 10; done; return 1; }
# the test client, from this checkout
client() { python3 "$LMCD/tests/vllm_reboot_ttft.py" "$VLLM_URL" "$@"; }
