#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Phase B sweep: does removing the H2D copy actually shorten the time to fetch
# a KV working set? Sweeps chunk size and concurrency for the three arms that
# matter and prints the median of REPS runs.
#
#   ./bench_dfs_gpu_lat.sh [pool] [cont] [ws_MiB]
#
# The decision column is total_ms -- the whole working set, i.e. this path's
# contribution to TTFT at that concurrency. Per-chunk percentiles explain it;
# they do not decide it. A path can win on per-chunk latency and still lose on
# total_ms if its bandwidth is lower, which is exactly the tension here: the
# GPU path has no copy but writes into GPU BAR at a per-QP limit.
#
# 'pinned' is in the sweep as a control, not a candidate: it is staging with
# the copy deleted and the data left in the wrong place, so it bounds how much
# removing the copy could possibly buy. If gpu does not beat pinnedcopy by
# roughly (pinnedcopy - pinned), the GPU path is paying its bandwidth deficit
# back out.
#
# p50 is unreliable here at a single run -- the per-chunk distribution is
# bimodal, so p50 jumps between modes and moved 25% between two identical
# runs. Medians across REPS, and means, are stable. Do not read a single p50.
set -euo pipefail

POOL=${1:-gdspool}
CONT=${2:-crp2g4}
WS=${3:-1024}
REPS=${REPS:-3}
CHUNKS=${CHUNKS:-"256 1024 4096 16384"}
WORKERS=${WORKERS:-"1 4 16"}
ARMS=${ARMS:-"pinned pinnedcopy gpu"}
BIN=${BIN:-./bench_dfs_gpu_lat}

[ -x "$BIN" ] || { echo "build $BIN first (see its header)" >&2; exit 1; }

# median of stdin numbers
med() { sort -n | awk '{v[NR]=$1} END {print (NR%2) ? v[(NR+1)/2] : (v[NR/2]+v[NR/2+1])/2}'; }
get() { sed -n "s/.*$2=\([0-9.]*\).*/\1/p" <<<"$1"; }

printf '%-7s %-7s %-5s %8s %10s %10s %10s %9s\n' \
	chunkKiB workers arm GB/s total_ms tot_mean tot_p95 copy_p50
for ch in $CHUNKS; do
	for nw in $WORKERS; do
		for a in $ARMS; do
			gbs=(); tms=(); tmn=(); p95=(); cp5=()
			for _ in $(seq "$REPS"); do
				line=$("$BIN" "$POOL" "$CONT" "$a" "$ch" "$WS" "$nw" 2>/dev/null) || continue
				gbs+=("$(get "$line" 'GB\/s')")
				tms+=("$(get "$line" total_ms)")
				tmn+=("$(get "$line" tot_mean)")
				p95+=("$(get "$line" tot_p95)")
				cp5+=("$(get "$line" copy_p50)")
				sleep 1
			done
			[ ${#tms[@]} -gt 0 ] || { echo "  $ch/$nw/$a: all reps failed" >&2; continue; }
			printf '%-7s %-7s %-5s %8s %10s %10s %10s %9s\n' \
				"$ch" "$nw" "$a" \
				"$(printf '%s\n' "${gbs[@]}" | med)" \
				"$(printf '%s\n' "${tms[@]}" | med)" \
				"$(printf '%s\n' "${tmn[@]}" | med)" \
				"$(printf '%s\n' "${p95[@]}" | med)" \
				"$(printf '%s\n' "${cp5[@]}" | med)"
		done
	done
done
