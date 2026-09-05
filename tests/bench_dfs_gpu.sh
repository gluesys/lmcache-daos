#!/usr/bin/env bash
#
# Run the bench_dfs_gpu arms under perf and print one table.
#
# Two perf invocations per arm, because the counters have different scopes:
#   - cycles are per-process, so they can be attached to the workload
#   - uncore_imc/* are uncore PMUs and only exist system-wide, so they are
#     collected with -a for exactly the workload's lifetime. That means they
#     include whatever else the box is doing -- run this on an idle host and
#     read the idle baseline printed first before trusting small differences.
#
#   ./bench_dfs_gpu.sh [pool] [cont] [chunk_MiB] [total_MiB] [workers]
#
# For the concurrency sweep, loop this over workers -- one perf pair per point,
# so each point keeps its own attribution:
#   for w in 1 4 16 32; do ./bench_dfs_gpu.sh gdspool kvgds 32 8192 $w; done
set -euo pipefail

POOL=${1:-gdspool}
CONT=${2:-kvgds}
CH=${3:-32}
TOT=${4:-8192}
NW=${5:-1}
BIN=${BIN:-./bench_dfs_gpu}
IMC="uncore_imc/cas_count_read/,uncore_imc/cas_count_write/"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

[ -x "$BIN" ] || { echo "build bench_dfs_gpu first (see its header)" >&2; exit 1; }

val() { awk -F, -v pat="$2" '$3 ~ pat {print $1; exit}' "$1"; }

echo "idle baseline (2s, system-wide):"
perf stat -a -e "$IMC" -x, -o "$TMP/idle" -- sleep 2 2>/dev/null
awk -F, '$3 ~ /cas_count/ {printf "  %-30s %10s %s\n", $3, $1, $2}' "$TMP/idle"
echo

echo "workers: $NW"
printf '%-11s %7s %9s %9s %12s %12s %9s\n' \
	arm GB/s chunk_ms cyc/byte DRAM_rd_MiB DRAM_wr_MiB DRAM_x
for arm in gpu pinnedcopy hostcopy pinned host; do
	perf stat -e cycles -x, -o "$TMP/c.$arm" -- \
		"$BIN" "$POOL" "$CONT" "$arm" "$CH" "$TOT" "$NW" \
		>"$TMP/o.$arm" 2>/dev/null
	sleep 1
	perf stat -a -e "$IMC" -x, -o "$TMP/d.$arm" -- \
		"$BIN" "$POOL" "$CONT" "$arm" "$CH" "$TOT" "$NW" \
		>"$TMP/o2.$arm" 2>/dev/null

	gbs=$(grep -oE 'GB/s=[0-9.]+' "$TMP/o2.$arm" | cut -d= -f2)
	cms=$(grep -oE 'chunk_ms=[0-9.]+' "$TMP/o2.$arm" | cut -d= -f2)
	cyc=$(val "$TMP/c.$arm" '^cycles$')
	rd=$(val "$TMP/d.$arm" 'cas_count_read')
	wr=$(val "$TMP/d.$arm" 'cas_count_write')
	# cycles/byte over the delivered bytes, and host DRAM bytes moved per
	# byte delivered -- the two numbers the GPU path is supposed to cut.
	cpb=$(python3 -c "print(f'{$cyc/($TOT*1048576):.3f}')")
	ratio=$(python3 -c "print(f'{(($rd)+($wr))/$TOT:.2f}')")
	printf '%-11s %7s %9s %9s %12s %12s %9s\n' \
		"$arm" "$gbs" "$cms" "$cpb" "$rd" "$wr" "$ratio"
	sleep 1
done
