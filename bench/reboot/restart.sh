#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Usage: restart.sh <outdir> <rank> <node id>. vLLM started while a storage node is down. Deletes
# the vLLM pod, powers the node off, waits until DAOS marks the rank down, starts vLLM again
# (setup.sh vllm) and times its start-up. Then drives the client with the rank still down for HOLD s
# (240), powers the node on and drives on until the client's SECS (900) ends.
# Under the rank guard the store connects on a background thread: vLLM starts in its usual time,
# every request is a miss until the rank is back, and the cache comes on by itself a few seconds
# after the rank is Joined (in-process and MP mode, an ordinary rank and the MS leader alike).
H=$(dirname "$0"); . "$H/env.sh"; R=$1; DEAD=$2; NODE=$3; mkdir -p $R
kubectl delete pod -n $NS $VLLM_POD --ignore-not-found --wait=true >/dev/null
( while :; do echo "--- $(date +%T)"; D system query | tail -n +3 | tr -s ' ' | tr '\n' ';'; echo; sleep 15; done ) > $R/states.log 2>&1 &
MON=$!
echo "$(date +%T) power off $NODE"; power off $NODE
S=$(date +%s)
until D system query -r $DEAD | grep -E "^ *$DEAD " | grep -qi -E 'excluded|errored|unknown|stopped'; do
  [ $(( $(date +%s) - S )) -gt 180 ] && { echo "rank $DEAD not marked down"; break; }; sleep 5; done
echo "$(date +%T) rank $DEAD: $(D system query -r $DEAD | grep -E "^ *$DEAD " | tr -s ' ')"
"$H/setup.sh" vllm >/dev/null
T0=$(date +%s)
until curl -sf -m 5 $VLLM_URL/v1/models >/dev/null; do
  [ $(( $(date +%s) - T0 )) -gt ${READY_MAX:-900} ] && { echo "$(date +%T) vLLM not ready after ${READY_MAX:-900} s"; break; }
  sleep 5; done
echo "$(date +%T) vLLM ready after $(( $(date +%s) - T0 )) s"
client --prompts ${PROMPTS:-16} --words ${WORDS:-2000} --secs ${SECS:-900} --fresh-every ${FRESH_EVERY:-50} > $R/ttft.txt 2>&1 &
CL=$!
sleep ${HOLD:-240}
echo "$(date +%T) power on $NODE"; power on $NODE
wait_joined $DEAD
wait $CL
ssh $GPU_SSH "cd /var/log/pods/${NS}_${VLLM_POD}_*/vllm && for f in \$(ls -tr); do zcat -f \$f; done" | sed 's/\x1b\[[0-9;]*m//g' > $R/v.log 2>&1
grep -E 'S1BucketStore ready|connect failed' $R/v.log
kill $MON; echo DONE
