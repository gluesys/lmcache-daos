#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Usage: agg.sh <outdir> <pool> <rank> <node id>. Does VOS aggregation on the live ranks stop while
# one rank is down? Under the running vLLM load (which overwrites and punches), sample each live
# engine's per-pool aggregation/GC/space counters every 15 s (daos_metrics of engine 0, summed over
# targets): 90 s healthy, the node off for DOWN_SECS (240), and 180 s after the rank rejoins.
# Assumes one engine per server pod, named daos-server-<host>-0 (daos-operator).
H=$(dirname "$0"); . "$H/env.sh"; R=$1; PL=$2; DEAD=$3; NODE=$4; mkdir -p $R
U=$(D pool list -v | awk -v p=$PL '$1==p{print $2}')
HOSTOF=$(D system query -v | tail -n +3 | awk '{print $1, $4}')
RANKS=$(echo "$HOSTOF" | awk '{print $1}')
sample() {
  for r in $RANKS; do [ $r = $DEAD ] && continue
    h=$(echo "$HOSTOF" | awk -v r=$r '$1==r{print $2}' | tr -d /); p=daos-server-$h-0
    timeout 20 kubectl exec -n $NS $p -- sh -c "daos_metrics -S 0 -C 2>/dev/null | grep '$U'" 2>/dev/null | python3 -c "
import sys,collections
s=collections.Counter()
for l in sys.stdin:
  f=l.strip().split(',')
  name=f[0].split('/$U/')[-1].rsplit('/',1)[0]
  if name=='vos_aggregation/epr_duration': s['epr_scans']+=int(f[5])
  elif name in ('vos_aggregation/dkey_deleted','vos_aggregation/deleted_ev','vos_aggregation/deleted_sv','vos_aggregation/obj_scanned','vos_aggregation/agg_blocked','vos_aggregation/fail_count','vos_gc/dkey_del','vos_gc/ev_del','vos_space/nvme_used'):
    s[name.split('/')[-1]]+=int(float(f[1]))
print('rank$r', ' '.join('%s=%d'%kv for kv in sorted(s.items())))"
  done
}
( while :; do echo "--- $(date +%T)"; sample; sleep 15; done ) > $R/agg.log 2>&1 &
MON=$!
sleep 90
echo "$(date +%T) power off $NODE"; power off $NODE
sleep ${DOWN_SECS:-240}
echo "$(date +%T) power on $NODE"; power on $NODE
wait_joined $DEAD
sleep 180
kill $MON; echo DONE
