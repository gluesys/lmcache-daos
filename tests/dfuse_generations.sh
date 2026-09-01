#!/usr/bin/env bash
#
# Independent check of the overwrite-generation corruption, using no code of
# ours: dfuse + cp + md5sum only.
#
# The direct dfs_sys reproducer says that after a few single-threaded overwrite
# generations, reading a file back returns a 4 MiB chunk of some OTHER file, or
# of a PREVIOUS generation. That claim is large enough that it should not rest
# on our own reader, so this repeats it through the POSIX path with coreutils.
#
# Design detail that matters: each file gets a DIFFERENT 28 MiB pattern, and the
# assignment rotates every generation. Writing the same bytes everywhere would
# make both failure modes invisible -- a stale generation and a neighbour's data
# would both still match. With rotation, a wrong read matches some other known
# pattern, and the script reports WHICH, so the failure names its own source.
#
#   ./dfuse_generations.sh <mountpoint> <generations> [files]
set -u

MNT=${1:?mountpoint}
GENS=${2:-8}
NFILES=${3:-16}
PATS=/dev/shm/pats

mkdir -p "$PATS"
declare -a md5
for i in $(seq 0 $((NFILES - 1))); do
	[ -f "$PATS/p$i" ] || dd if=/dev/urandom of="$PATS/p$i" bs=1M count=28 status=none
	md5[$i]=$(md5sum "$PATS/p$i" | cut -d' ' -f1)
done

whose() {                      # which pattern do these bytes belong to?
	local sum=$1 i
	for i in $(seq 0 $((NFILES - 1))); do
		[ "$sum" = "${md5[$i]}" ] && { echo "pattern p$i"; return; }
	done
	echo "no known pattern"
}

total_bad=0
for g in $(seq 1 "$GENS"); do
	for i in $(seq 0 $((NFILES - 1))); do
		cp "$PATS/p$(( (i + g) % NFILES ))" "$MNT/f$i"
	done
	bad=0
	detail=""
	for i in $(seq 0 $((NFILES - 1))); do
		want=$(( (i + g) % NFILES ))
		got=$(md5sum "$MNT/f$i" | cut -d' ' -f1)
		if [ "$got" != "${md5[$want]}" ]; then
			bad=$((bad + 1))
			detail="$detail\n    f$i: expected pattern p$want, got $(whose "$got")"
		fi
	done
	total_bad=$((total_bad + bad))
	printf 'gen %-2d  %d/%d files wrong' "$g" "$bad" "$NFILES"
	[ "$bad" -gt 0 ] && printf '%b' "$detail"
	printf '\n'
done

echo
echo "total: $total_bad/$((GENS * NFILES)) reads wrong via dfuse + md5sum"
