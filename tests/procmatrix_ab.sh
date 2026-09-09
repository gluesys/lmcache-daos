#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Thread-vs-process crossover (plan §4 P0 #3): does the corruption need
# client-process-shared state?
#
#   arm T: 1 process × 16 threads × R rounds   (the usual reproducer shape)
#   arm P: 16 processes × 1 thread × R rounds  (nothing shared but the node)
#
# Both arms perform 16×R reads against the same container. Arm P gives every
# process a distinct tid base (-T) — without that all processes would tag
# their payloads tid 0 and a cross-process chunk substitution would verify
# as correct, making the arm blind by construction (see dfs_integrity.c).
#
# Same statistical rules as crossover_ab.sh: balanced AB BA BA AB order,
# stderr preserved per run, run-level sign-flip permutation as the verdict.
#
#   ./procmatrix_ab.sh <binary> <pool> <cont> <pairs> <outdir> [rounds]
set -u

BIN=${1:?binary}
POOL=${2:?pool}
CONT=${3:?container}
PAIRS=${4:?pairs}
OUT=${5:?outdir}
ROUNDS=${6:-40}
NPROC=16
PAUSE=${PAUSE:-2}

mkdir -p "$OUT"
: >"$OUT/runs.tsv"
echo -e "pair\tslot\tarm\tbad\ttotal\trate" >>"$OUT/runs.tsv"

record() {
	local pair=$1 slot=$2 arm=$3 b=$4 t=$5
	echo -e "${pair}\t${slot}\t${arm}\t${b}\t${t}\t$(awk -v b="$b" -v t="$t" 'BEGIN{print t? b/t : 0}')" >>"$OUT/runs.tsv"
	printf '  pair %-2d %s arm %s: %s/%s\n' "$pair" "$slot" "$arm" "$b" "$t"
	sleep "$PAUSE"
}

run_T() {
	local pair=$1 slot=$2 log="$OUT/pair${1}_${2}_T.log" line frac
	NA_UCX_EXTRA_TLS= "$BIN" -p "$POOL" -c "$CONT" -s 28 -t 16 -r "$ROUNDS" \
		>"$log" 2>&1
	line=$(grep -E '^(PASS|FAIL):' "$log")
	frac=$(echo "$line" | awk '{print $2}')
	record "$pair" "$slot" T "${frac%%/*}" "${frac##*/}"
}

run_P() {
	local pair=$1 slot=$2 b=0 t=0 line frac
	local pids=()
	for i in $(seq 0 $((NPROC - 1))); do
		NA_UCX_EXTRA_TLS= "$BIN" -p "$POOL" -c "$CONT" -s 28 -t 1 \
			-r "$ROUNDS" -T "$i" \
			>"$OUT/pair${pair}_${slot}_P_proc${i}.log" 2>&1 &
		pids+=($!)
	done
	wait "${pids[@]}"
	for i in $(seq 0 $((NPROC - 1))); do
		line=$(grep -E '^(PASS|FAIL):' "$OUT/pair${pair}_${slot}_P_proc${i}.log")
		frac=$(echo "$line" | awk '{print $2}')
		b=$((b + ${frac%%/*}))
		t=$((t + ${frac##*/}))
	done
	record "$pair" "$slot" P "$b" "$t"
}

orders=("T P" "P T" "P T" "T P")
echo "pairs=$PAIRS rounds=$ROUNDS out=$OUT (T=1x16 threads, P=16x1 processes)"
for p in $(seq 1 "$PAIRS"); do
	order=${orders[$(( (p - 1) % 4 ))]}
	set -- $order
	for slot in 1st 2nd; do
		arm=$1; shift
		[ "$arm" = T ] && run_T "$p" "$slot" || run_P "$p" "$slot"
	done
done

python3 - "$OUT/runs.tsv" <<'PY'
import itertools, statistics, sys

rows = [l.split('\t') for l in open(sys.argv[1]).read().splitlines()[1:]]
runs = {}
for pair, slot, arm, bad, total, rate in rows:
    runs.setdefault(arm, {})[int(pair)] = (int(bad), int(total), float(rate))

for arm in sorted(runs):
    rates = [r for _, _, r in runs[arm].values()]
    bad = sum(b for b, _, _ in runs[arm].values())
    tot = sum(t for _, t, _ in runs[arm].values())
    print(f"arm {arm}: pooled {bad}/{tot} (descriptive) | run rates "
          f"median={statistics.median(rates):.4f} max={max(rates):.4f} "
          f"zero-runs={sum(1 for r in rates if r == 0)}/{len(rates)}")

pairs = sorted(set.intersection(*[set(v) for v in runs.values()]))
arms = sorted(runs)
diffs = [runs[arms[0]][p][2] - runs[arms[1]][p][2] for p in pairs]
obs, n = sum(diffs), len(diffs)
perms = [sum(d * sg for d, sg in zip(diffs, signs))
         for signs in itertools.product((1, -1), repeat=n)]
p_two = sum(1 for v in perms if abs(v) >= abs(obs) - 1e-12) / len(perms)
print(f"paired diff ({arms[0]}-{arms[1]}): mean={obs/n:+.4f} over {n} pairs, "
      f"sign-flip permutation p={p_two:.4f} (exact, two-sided)")
PY
