#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Usage: reboot.sh <outdir> <rank> <node id>. vLLM TTFT while the storage node of <rank> reboots.
# The vLLM pod must be running. 60 s after warm-up the node is powered off (power off, not a
# clean shutdown), held off for DOWN_SECS (240), powered on, and watched until it is Joined; the
# client runs for SECS (900) in all. PQ=<pool> also logs that pool's disabled targets and rebuild
# state (an rd_fac >= 1 pool rebuilds). Output: ttft.txt (client), states.log, v.log (vLLM log).
H=$(dirname "$0"); . "$H/env.sh"; R=$1; DEAD=$2; NODE=$3; mkdir -p $R
( while :; do echo "--- $(date +%T)"; D system query | tail -n +3 | tr -s ' ' | tr '\n' ';'; echo
  [ -n "$PQ" ] && D pool query $PQ | grep -E 'disabled|Rebuild|Free' | tr -s ' ' | tr '\n' ';' && echo; sleep 15; done ) > $R/states.log 2>&1 &
MON=$!
client --prompts ${PROMPTS:-16} --words ${WORDS:-2000} --secs ${SECS:-900} --fresh-every ${FRESH_EVERY:-50} > $R/ttft.txt 2>&1 &
CL=$!
until grep -q warmed $R/ttft.txt; do sleep 2; kill -0 $CL 2>/dev/null || { echo client died; kill $MON; exit 1; }; done
echo "$(date +%T) warmed"; sleep 60
echo "$(date +%T) power off $NODE"; power off $NODE
sleep ${DOWN_SECS:-240}
echo "$(date +%T) power on $NODE"; power on $NODE
wait_joined $DEAD
wait $CL
# rotated (gzip) files too
ssh $GPU_SSH "cd /var/log/pods/${NS}_${VLLM_POD}_*/vllm && for f in \$(ls -tr); do zcat -f \$f; done" | sed 's/\x1b\[[0-9;]*m//g' > $R/v.log 2>&1
kill $MON; echo DONE
