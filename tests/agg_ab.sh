#!/usr/bin/env bash
#
# Does VOS aggregation cause the silent read corruption?
#
# Run on cell1 (needs dmg); drives the reproducer on client-5. Alternates the
# pool's reclaim strategy between "lazy" (aggregation on) and "disabled"
# (aggregation off) in blocks, and pools the per-read counts per arm.
#
# Why this arm exists: the engine logs DER_CSUM failures from
# vos_csum_recalc.c:csum_agg_verify() while aggregating exactly the 4 MiB
# windows whose size matches the corrupt region the client sees, on both
# ranks, while every NVMe device reports zero media/read/write errors.
#
# Why blocks and not one long run: under sustained load the writes start
# failing with DER_MISC (EIO), which truncates a run and destroys the sample
# size. Short runs keep most of them intact; the count actually completed is
# read back from the summary line rather than assumed.
#
#   ./agg_ab.sh <pool> <container> [runs_per_block] [blocks]
set -u

POOL=${1:?pool}
CONT=${2:?container}
RUNS=${3:-5}
BLOCKS=${4:-2}
DMG=${DMG:-/opt/daos-gds/bin/dmg}
CLIENT=${CLIENT:-client-5}
BIN=${BIN:-./dfs_integrity_stock}

on_bad=0; on_tot=0; off_bad=0; off_tot=0

block() {
	local label=$1 n=$2 b t frac line
	for i in $(seq 1 "$n"); do
		line=$(ssh "$CLIENT" "cd /root && NA_UCX_EXTRA_TLS= $BIN -p $POOL -c $CONT -s 28 -t 16 -r 10 2>/dev/null | grep -E '^(PASS|FAIL):'")
		frac=$(echo "$line" | awk '{print $2}')
		b=${frac%%/*}; t=${frac##*/}
		[ -z "$b" ] && b=0
		[ -z "$t" ] && t=0
		printf '  %-8s run %d: %s/%s\n' "$label" "$i" "$b" "$t"
		if [ "$label" = "agg-ON" ]; then
			on_bad=$((on_bad + b)); on_tot=$((on_tot + t))
		else
			off_bad=$((off_bad + b)); off_tot=$((off_tot + t))
		fi
	done
}

for r in $(seq 1 "$BLOCKS"); do
	"$DMG" -i pool set-prop "$POOL" reclaim:lazy >/dev/null 2>&1
	echo "block $r: reclaim=lazy (aggregation ON)"
	block agg-ON "$RUNS"
	"$DMG" -i pool set-prop "$POOL" reclaim:disabled >/dev/null 2>&1
	echo "block $r: reclaim=disabled (aggregation OFF)"
	block agg-OFF "$RUNS"
done

# Leave aggregation on: with reclaim disabled the pool never reclaims space.
"$DMG" -i pool set-prop "$POOL" reclaim:lazy >/dev/null 2>&1

echo
echo "aggregation ON : $on_bad/$on_tot reads corrupt"
echo "aggregation OFF: $off_bad/$off_tot reads corrupt"
