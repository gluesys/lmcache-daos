#!/usr/bin/env bash
#
# Balanced-crossover A/B for two environment configurations of the same
# reproducer. Replaces dfs_integrity_ab.sh for arm comparisons, fixing the
# three defects /tmp/daos-fix.md §9 identified in it:
#
#   1. Order was always A-then-B, confounding the arm with time and with the
#      burst structure of the failures. Here the pair order follows the
#      balanced pattern AB BA BA AB (repeating), so each arm leads equally.
#   2. stderr went to /dev/null, discarding EIO, DER_HG and loader errors.
#      Here every run's stdout+stderr is preserved in its own file.
#   3. Failures arrive in bursts, so pooled per-read Wilson intervals assume
#      an independence that does not hold. The verdict here is computed at
#      run level: paired rate differences with an exact sign-flip
#      permutation test, plus per-run medians/IQR, and pooled counts are
#      printed only as descriptive context.
#
# Arms are defined by environment variable strings so the same harness works
# for MR-cache, buffer-strategy or any future client-side knob:
#
#   ARM_A_ENV="NA_UCX_EXTRA_TLS=" \
#   ARM_B_ENV="NA_UCX_EXTRA_TLS= CRT_MRC_ENABLE=0 UCX_RCACHE_ENABLE=n" \
#   ./crossover_ab.sh <binary> <pool> <cont> <pairs> <outdir> [extra args...]
#
# Env vars must take effect before daos_init(), which is why they are passed
# on the command line of each run rather than exported once: UCX reads its
# configuration at init and ignores later changes.
set -u

BIN=${1:?binary}
POOL=${2:?pool}
CONT=${3:?container}
PAIRS=${4:?pairs}
OUT=${5:?outdir}
shift 5
EXTRA=("$@")
[ ${#EXTRA[@]} -eq 0 ] && EXTRA=(-s 28 -t 16 -r 40)

ARM_A_ENV=${ARM_A_ENV:?set ARM_A_ENV}
ARM_B_ENV=${ARM_B_ENV:?set ARM_B_ENV}
PAUSE=${PAUSE:-2}

mkdir -p "$OUT"
: >"$OUT/runs.tsv"
echo -e "pair\tslot\tarm\tbad\ttotal\trate" >>"$OUT/runs.tsv"

run_one() {
	local pair=$1 slot=$2 arm=$3 envs log frac line
	envs=$ARM_A_ENV
	[ "$arm" = B ] && envs=$ARM_B_ENV
	log="$OUT/pair${pair}_${slot}_${arm}.log"
	# shellcheck disable=SC2086
	env $envs "$BIN" -p "$POOL" -c "$CONT" "${EXTRA[@]}" >"$log" 2>&1
	line=$(grep -E '^(PASS|FAIL):' "$log")
	frac=$(echo "$line" | awk '{print $2}')
	local b=${frac%%/*} t=${frac##*/}
	[ -z "$b" ] && b=0
	[ -z "$t" ] && t=0
	echo -e "${pair}\t${slot}\t${arm}\t${b}\t${t}\t$(awk -v b="$b" -v t="$t" 'BEGIN{print t? b/t : 0}')" >>"$OUT/runs.tsv"
	printf '  pair %-2d %s arm %s: %s/%s\n' "$pair" "$slot" "$arm" "$b" "$t"
	sleep "$PAUSE"
}

orders=("A B" "B A" "B A" "A B")
echo "pairs=$PAIRS args=${EXTRA[*]} out=$OUT"
echo "armA: $ARM_A_ENV"
echo "armB: $ARM_B_ENV"
for p in $(seq 1 "$PAIRS"); do
	order=${orders[$(( (p - 1) % 4 ))]}
	set -- $order
	run_one "$p" 1st "$1"
	run_one "$p" 2nd "$2"
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
    q = statistics.quantiles(rates, n=4) if len(rates) >= 4 else [0, 0, 0]
    print(f"arm {arm}: pooled {bad}/{tot} (descriptive only) | run rates "
          f"median={statistics.median(rates):.4f} IQR=[{q[0]:.4f},{q[2]:.4f}] "
          f"max={max(rates):.4f} zero-runs={sum(1 for r in rates if r == 0)}/{len(rates)}")

pairs = sorted(set(runs['A']) & set(runs['B']))
diffs = [runs['A'][p][2] - runs['B'][p][2] for p in pairs]
obs = sum(diffs)
n = len(diffs)
if n <= 20:
    perms = [sum(d * s for d, s in zip(diffs, signs))
             for signs in itertools.product((1, -1), repeat=n)]
    p_two = sum(1 for v in perms if abs(v) >= abs(obs) - 1e-12) / len(perms)
    kind = "exact"
else:
    import random
    rnd = random.Random(20260901)
    perms = [sum(d * rnd.choice((1, -1)) for d in diffs) for _ in range(100000)]
    p_two = sum(1 for v in perms if abs(v) >= abs(obs) - 1e-12) / len(perms)
    kind = "monte-carlo"
print(f"paired diff (A-B): mean={obs/n:+.4f} over {n} pairs, "
      f"sign-flip permutation p={p_two:.4f} ({kind}, two-sided)")
print("NOTE: pooled counts are descriptive; the permutation p is the verdict.")
PY
