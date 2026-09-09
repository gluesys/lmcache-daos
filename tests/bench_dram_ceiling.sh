#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Measure the host DRAM ceiling twice over: once by the kernel's own byte
# accounting, once by the memory controllers. The second one is the number to
# divide by, because it is the same instrument the load-side measurements used.
#
#   ./bench_dram_ceiling.sh [array_MiB] [scope]
#     scope: node (default, both sockets) | sock0 | sock1
#
# Method note -- why each kernel runs twice:
#
# perf can only attach to the whole process, but the process also does a NUMA
# first-touch pass over all three arrays and one untimed warm-up iteration.
# Attributing those to the kernel would distort the ceiling. So each kernel
# runs once with 0 timed iterations (init + warm-up only) and once with N, and
# the wrapper subtracts the byte counts:
#
#     imc_GBs = (IMC_bytes_N - IMC_bytes_0) / timed_s_N
#
# Only BYTES are differenced. Byte counts are deterministic, so init and
# warm-up cancel exactly. The denominator is the program's own measurement of
# the timed iterations. An earlier version differenced wall clocks between an
# N-iteration and a 2N-iteration run as well; separate processes vary enough
# (page placement, turbo residency) that this reported copy at 642 GB/s, above
# the DIMMs' 614 GB/s theoretical peak, and read at imc/counted = 0.88, which
# is impossible for a kernel with no stores. If those two tells reappear, the
# subtraction has gone wrong again -- do not publish the number.
#
# Expect imc_GBs > counted_GBs for every kernel with a store (copy, scale, add,
# triad): an ordinary x86 store pulls the destination line in before writing
# it, so a Copy moves 3 array-sizes through DRAM while STREAM credits it 2.
# The 'read' kernel has no stores, so there the two should agree -- that
# agreement is the check that the instrument is wired up correctly. If 'read'
# disagrees by more than a few percent, stop and fix that before trusting
# anything else here.
set -euo pipefail

MIB=${1:-1024}
SCOPE=${2:-node}
N=${N:-10}
BIN=${BIN:-./bench_dram_ceiling}
IMC="uncore_imc/cas_count_read/,uncore_imc/cas_count_write/"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

[ -x "$BIN" ] || { echo "build first: gcc -O3 -march=native -fopenmp -o bench_dram_ceiling bench_dram_ceiling.c" >&2; exit 1; }

case $SCOPE in
node)  PIN=() ;;
sock0) PIN=(numactl -N0 -m0) ;;
sock1) PIN=(numactl -N1 -m1) ;;
*)     echo "scope: node|sock0|sock1" >&2; exit 2 ;;
esac

# perf already applies cas_count_*'s scale factor and prints a unit in field 2
# (MiB here), so the raw value is NOT a cache-line count -- multiplying by 64
# undercounts by ~10^4 and every ratio comes out 0.00. Honour the unit field
# rather than assuming one, since perf picks the unit from the magnitude.
imc() {
	awk -F, '
		$3 ~ /cas_count/ {
			u = $2
			m = (u == "MiB") ? 1048576 : \
			    (u == "GiB") ? 1073741824 : \
			    (u == "KiB") ? 1024 : \
			    (u == "B" || u == "") ? 1 : -1
			if (m < 0) { print "BADUNIT:" u > "/dev/stderr"; exit 1 }
			s += $1 * m
		}
		END { printf "%.0f", s }' "$1"
}

echo "scope=$SCOPE array_MiB=$MIB timed_iters=$N,$((N*2))"
echo
echo "idle baseline (3s, system-wide):"
perf stat -a -e "$IMC" -x, -o "$TMP/idle" -- sleep 3 2>/dev/null
idle_bytes=$(imc "$TMP/idle")
printf '  %.2f GB/s of DRAM traffic with no load\n\n' \
	"$(python3 -c "print($idle_bytes/3/1e9)")"

printf '%-7s %8s %11s %11s %10s %9s %8s\n' \
	kernel threads counted_GBs imc_GBs imc/counted best_GBs timed_s
for k in read copy scale add triad; do
	for it in 0 "$N"; do
		perf stat -a -e "$IMC" -x, -o "$TMP/p.$k.$it" -- \
			"${PIN[@]}" "$BIN" "$k" "$MIB" "$it" \
			>"$TMP/o.$k.$it" 2>/dev/null
		sleep 2
	done

	thr=$(head -1 "$TMP/o.$k.$N" | sed -n 's/.*threads=\([0-9]*\).*/\1/p')
	best=$(tail -1 "$TMP/o.$k.$N" | sed -n 's/.*best_GBs=\([0-9.]*\).*/\1/p')
	gbn=$(tail -1 "$TMP/o.$k.$N"  | sed -n 's/.*timed_counted_GB=\([0-9.]*\).*/\1/p')
	sn=$(tail -1 "$TMP/o.$k.$N"   | sed -n 's/.*timed_s=\([0-9.]*\).*/\1/p')
	b0=$(imc "$TMP/p.$k.0")
	bn=$(imc "$TMP/p.$k.$N")

	read -r cbs ibs ratio <<<"$(python3 - "$gbn" "$sn" "$b0" "$bn" <<'PY'
import sys
gbn, sn, b0, bn = (float(x) for x in sys.argv[1:5])
if sn <= 0 or bn <= b0:
    print("nan nan nan"); raise SystemExit
counted = gbn / sn                  # GB/s the kernel credits itself
imc = (bn - b0) / sn / 1e9          # GB/s the controllers actually moved
print(f"{counted:.1f} {imc:.1f} {imc/counted:.2f}")
PY
)"
	printf '%-7s %8s %11s %11s %10s %9s %8s\n' \
		"$k" "${thr:-?}" "$cbs" "$ibs" "$ratio" "${best:-?}" "$sn"
done

cat <<-'EOF'

	Read imc_GBs as the ceiling; counted_GBs is only there to be compared
	against it. Checks that must hold before quoting any of this:
	  - read: imc/counted ~= 1.00, and it approaches 1.00 from below only as
	    the arrays outgrow L3. This host has 520 MiB of L3, so 4 GiB arrays
	    leave ~12% resident and read reads 0.91; at 32 GiB it should be
	    ~0.98. If it is low at large arrays, the counters are missing
	    channels -- check that uncore_imc_0..7 all report.
	  - store kernels: imc/counted ~= 1.00 on this generation, NOT 1.50.
	    There are no non-temporal stores in the binary (objdump: no movnt),
	    so the classical expectation would be 1.50 from read-for-ownership;
	    Emerald Rapids elides the ownership read for full-line sequential
	    writes, which is what these kernels do. 1.50 here would mean the
	    stores are not being coalesced into full lines.
	  - every imc_GBs <= 614 GB/s (16ch x DDR5-4800 x 8B)
	Anything over 614 means the measurement is broken rather than the memory
	being fast.

	Which row to divide by: staging moves a byte into host DRAM (NIC DMA
	write) and back out of it (GPU copy engine read), so the 50/50 mix in
	'copy' is the representative ceiling, not the pure-read row.
EOF
