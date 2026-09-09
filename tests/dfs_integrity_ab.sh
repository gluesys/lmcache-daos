#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Interleaved A/B between two DAOS client builds, with pooled totals.
#
# Interleaved, not one arm after the other: the failure rate moves with server
# state (load, checkpointing, what was written before), so back-to-back blocks
# can differ by more than the arms do. This is the mistake §11 of
# DAOS-CONCURRENT-READ-CORRUPTION.md records repeatedly.
#
#   ./dfs_integrity_ab.sh <binA> <binB> <pool> <cont> <reps> [extra args...]
#
# Prints one line per run and a pooled summary per arm. Compare the arms by
# their Wilson intervals -- a 0/640 run is not evidence of a fixed build.
set -u

A=${1:?binary A}
B=${2:?binary B}
POOL=${3:?pool}
CONT=${4:?container}
REPS=${5:?reps}
shift 5

export NA_UCX_EXTRA_TLS=

declare -A bad tot
for arm in A B; do bad[$arm]=0; tot[$arm]=0; done

for i in $(seq 1 "$REPS"); do
	for arm in A B; do
		binary=$A
		[ "$arm" = B ] && binary=$B
		out=$("$binary" -p "$POOL" -c "$CONT" "$@" 2>/dev/null)
		line=$(echo "$out" | grep -E "^(PASS|FAIL):")
		# "FAIL: 10/1040 concurrent reads corrupt (...)"
		frac=$(echo "$line" | awk '{print $2}')
		b=${frac%%/*}
		t=${frac##*/}
		bad[$arm]=$(( ${bad[$arm]} + b ))
		tot[$arm]=$(( ${tot[$arm]} + t ))
		printf 'rep %-2d arm %s (%s): %s\n' "$i" "$arm" "$(basename "$binary")" "$line"
		echo "$out" | grep -E "^  (CORRUPT|SHORT)" | head -3 | sed 's/^/      /'
	done
done

echo
for arm in A B; do
	binary=$A
	[ "$arm" = B ] && binary=$B
	python3 - "${bad[$arm]}" "${tot[$arm]}" "$arm" "$(basename "$binary")" <<'PY'
import math, sys
k, n, arm, name = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
z = 1.959964
if n:
    p = k / n
    z2n = z * z / n
    c = p + z2n / 2
    m = z * math.sqrt(p * (1 - p) / n + z2n / (4 * n))
    lo, hi = max(0.0, (c - m) / (1 + z2n)), min(1.0, (c + m) / (1 + z2n))
else:
    p = lo = hi = 0.0
print(f"arm {arm} {name:26s} {k:4d}/{n:5d} reads corrupt = {100*p:.3f}% "
      f"(95% CI {100*lo:.3f}-{100*hi:.3f}%)")
PY
done
