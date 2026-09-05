#!/usr/bin/env bash
#
# Does VOS aggregation cause the silent read corruption?
#
# Run on cell1 (needs dmg); drives tests/dfs_integrity.c on a client. Alternates
# the pool's reclaim strategy between "lazy" (aggregation on) and "disabled"
# (aggregation off) in blocks, and pools per-read counts per arm with a Wilson
# interval.
#
# Why this arm exists: the engine logs DER_CSUM out of
# vos_csum_recalc.c:csum_agg_verify() while aggregating exactly the 4 MiB
# windows whose size matches the corrupt region the client sees, on both ranks,
# while every NVMe reports zero media/read/write errors.
#
# Three things this harness does that the first version did not, each because
# the first version produced an unusable measurement:
#
#   - blocks, alternating, several of them. The failure rate drifts with server
#     state by more than the arms differ, so a single ON block followed by a
#     single OFF block measures the drift (§11 of the handoff records this
#     mistake repeatedly).
#   - short runs with a pause. Under sustained 16-thread load the writes start
#     failing with DER_MISC (EIO) and the run dies early, which silently guts
#     the sample. Reads actually completed are read back from the summary line,
#     never assumed, and runs that complete nothing are counted and reported.
#   - a positive control for the knob itself. With reclaim disabled the pool
#     must stop reclaiming space, so NVMe free is sampled per block: if it
#     falls during OFF blocks and recovers during ON blocks, the toggle worked.
#     Without this, "OFF was clean" could just mean the property never applied.
#
#   ./agg_ab.sh <pool> <container> [blocks_per_arm] [runs_per_block] [rounds]
set -u

POOL=${1:?pool}
CONT=${2:?container}
BLOCKS=${3:-4}
RUNS=${4:-8}
ROUNDS=${5:-10}
DMG=${DMG:-/opt/daos-gds/bin/dmg}
CLIENT=${CLIENT:-client-5}
BIN=${BIN:-./dfs_integrity_stock}
THREADS=${THREADS:-16}
SETTLE=${SETTLE:-15}            # let aggregation state match the arm before measuring
PAUSE=${PAUSE:-2}               # between runs, to keep DER_MISC from truncating
# Which arm goes first inside each block. Run the experiment BOTH ways and pool:
# with one fixed order, "first in the block" is confounded with the arm, and a
# warm-up or drain effect would masquerade as an aggregation effect.
ORDER=${ORDER:-on-first}

on_bad=0; on_tot=0; on_dead=0
off_bad=0; off_tot=0; off_dead=0

nvme_free() {
	"$DMG" -i pool query "$POOL" 2>/dev/null |
		awk '/Storage tier 1/{f=1} f && /Free:/{print $2 $3; exit}'
}

block() {
	local label=$1 b t frac line
	for i in $(seq 1 "$RUNS"); do
		line=$(ssh "$CLIENT" "cd /root && NA_UCX_EXTRA_TLS= $BIN -p $POOL -c $CONT \
			-s 28 -t $THREADS -r $ROUNDS 2>/dev/null | grep -E '^(PASS|FAIL):'")
		frac=$(echo "$line" | awk '{print $2}')
		b=${frac%%/*}; t=${frac##*/}
		[ -z "$b" ] && b=0
		[ -z "$t" ] && t=0
		printf '    %-7s run %-2d %4s/%-4s\n' "$label" "$i" "$b" "$t"
		if [ "$label" = agg-ON ]; then
			on_bad=$((on_bad + b)); on_tot=$((on_tot + t))
			[ "$t" -lt $((THREADS * ROUNDS)) ] && on_dead=$((on_dead + 1))
		else
			off_bad=$((off_bad + b)); off_tot=$((off_tot + t))
			[ "$t" -lt $((THREADS * ROUNDS)) ] && off_dead=$((off_dead + 1))
		fi
		sleep "$PAUSE"
	done
}

echo "pool=$POOL cont=$CONT blocks/arm=$BLOCKS runs/block=$RUNS rounds=$ROUNDS threads=$THREADS order=$ORDER"
echo "target per arm: $((BLOCKS * RUNS * ROUNDS * THREADS)) reads"

half_on() {
	"$DMG" -i pool set-prop "$POOL" reclaim:lazy >/dev/null 2>&1
	sleep "$SETTLE"
	echo "  block $1  reclaim=lazy      NVMe free before: $(nvme_free)"
	block agg-ON
	echo "  block $1  reclaim=lazy      NVMe free after:  $(nvme_free)"
}

half_off() {
	"$DMG" -i pool set-prop "$POOL" reclaim:disabled >/dev/null 2>&1
	sleep "$SETTLE"
	echo "  block $1  reclaim=disabled  NVMe free before: $(nvme_free)"
	block agg-OFF
	echo "  block $1  reclaim=disabled  NVMe free after:  $(nvme_free)"
}

for r in $(seq 1 "$BLOCKS"); do
	if [ "$ORDER" = off-first ]; then
		half_off "$r"; half_on "$r"
	else
		half_on "$r"; half_off "$r"
	fi
done

# Never leave the pool with reclaim disabled: it would stop reclaiming space.
"$DMG" -i pool set-prop "$POOL" reclaim:lazy >/dev/null 2>&1

echo
python3 - "$on_bad" "$on_tot" "$on_dead" "$off_bad" "$off_tot" "$off_dead" <<'PY'
import math, sys

def wilson(k, n):
    if not n:
        return 0.0, 0.0, 0.0
    z = 1.959964
    p = k / n
    z2n = z * z / n
    c = p + z2n / 2
    m = z * math.sqrt(p * (1 - p) / n + z2n / (4 * n))
    return p, max(0.0, (c - m) / (1 + z2n)), min(1.0, (c + m) / (1 + z2n))

on_k, on_n, on_d, off_k, off_n, off_d = (int(a) for a in sys.argv[1:7])
for name, k, n, d in (("aggregation ON ", on_k, on_n, on_d),
                      ("aggregation OFF", off_k, off_n, off_d)):
    p, lo, hi = wilson(k, n)
    print(f"{name}: {k:4d}/{n:6d} = {100*p:.3f}% (95% CI {100*lo:.3f}-{100*hi:.3f}%), "
          f"{d} truncated run(s)")

p_on, _, _ = wilson(on_k, on_n)
if off_n and on_k:
    # If OFF really behaved like ON, how surprising is the OFF count?
    exp = p_on * off_n
    print(f"\nAt the ON rate, OFF would be expected to show {exp:.1f} failures; "
          f"it showed {off_k}.")
    if off_k == 0 and exp:
        print(f"P(0 failures | ON rate) = {math.exp(-exp):.2e}")
PY
