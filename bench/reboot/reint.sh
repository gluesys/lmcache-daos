#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Usage: reint.sh <outdir> <pool> <rank>. Reintegrate a rank's targets under the vLLM load and log
# the pool's rebuild state every 10 s until it is idle with nothing disabled (at most 30 min). For an
# rd_fac >= 1 pool after reboot.sh: DAOS excluded the rank and rebuilt without it.
H=$(dirname "$0"); . "$H/env.sh"; R=$1; PL=$2; RK=$3; mkdir -p $R
client --prompts 32 --words 2000 --secs 1800 --fresh-every 200 > $R/ttft.txt 2>&1 &
CL=$!
until grep -q warmed $R/ttft.txt; do sleep 2; kill -0 $CL 2>/dev/null || { echo client died; exit 1; }; done; sleep 60
echo "$(date +%T) reintegrate"; D pool reintegrate $PL --ranks=$RK 2>&1 | tail -2
S=$(date +%s)
while [ $(( $(date +%s) - S )) -lt 1800 ]; do
  q=$(D pool query $PL | grep -E 'disabled|Rebuild|Free' | tr -s ' ' | tr '\n' ';'); echo "$(date +%T) $q"
  echo "$q" | grep -q 'disabled=0' && echo "$q" | grep -q -E 'Rebuild (idle|done)' && break; sleep 10
done
echo "$(date +%T) settled"; sleep 60; kill $CL; echo DONE
