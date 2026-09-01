#!/usr/bin/env bash
#
# Capture-until-corrupt: run the raw-object reproducer under tcpdump until a
# run reports corruption, keeping only that run's capture.
#
# Filter is server->client only (the fetch-response direction): write payloads
# flow client->server and the at-rest checks have long established that stored
# bytes are fine, so capturing them would double the disk and drop rate for
# nothing. obj_integrity is used instead of the DFS reproducer because its
# payload offsets are exactly file offsets (no 36-byte header skew) and its
# chunks are exactly the fetch boundaries.
#
#   ./cap_loop.sh <outdir> [tries] [rounds]
set -u

OUT=${1:?outdir}
TRIES=${2:-6}
ROUNDS=${3:-10}
IFACE=${IFACE:-ens255np0}
FILTER='tcp and (src host 192.168.10.82 or src host 192.168.10.84)'
BIN=${BIN:-/root/obj_integrity_28}

mkdir -p "$OUT"
for t in $(seq 1 "$TRIES"); do
	pcap="$OUT/try${t}.pcap"
	log="$OUT/try${t}.log"
	tcpdump -i "$IFACE" -s 0 -B 262144 -w "$pcap" $FILTER \
		>"$OUT/try${t}.tcpdump" 2>&1 &
	tpid=$!
	sleep 2
	NA_UCX_EXTRA_TLS= "$BIN" -p gdspool -c ci_obj -s 28 -k 4 -t 16 \
		-r "$ROUNDS" >"$log" 2>&1
	sleep 2
	kill -INT "$tpid" 2>/dev/null
	wait "$tpid" 2>/dev/null
	summary=$(grep -E '^(PASS|FAIL)' "$log")
	drops=$(grep -oE '[0-9]+ packets dropped' "$OUT/try${t}.tcpdump" | head -1)
	echo "try $t: $summary | $(du -h "$pcap" | cut -f1) | ${drops:-drops n/a}"
	if grep -q '^  CORRUPT' "$log"; then
		echo "KEPT: $pcap"
		exit 0
	fi
	rm -f "$pcap"
done
echo "no corruption in $TRIES tries"
exit 1
