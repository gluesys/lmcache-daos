#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Measure how often a cache hit returns wrong KV, with a confidence interval,
# and classify each failure.
#
# This exists because four causes were "identified" and then disproven on this
# bug, and every one of those calls rested on a handful of trials of an
# intermittent failure. A/B comparisons are worthless until the base rate is
# known: aliased 0/6 against copied 6/6 looked decisive, and a rerun of the
# copied build then scored 2/6.
#
#   ./kv_failure_rate.sh [trials] [max_tokens] [url]
#
# Each trial uses a fresh prose prompt (so pass A is a genuine miss), WAITS FOR
# THE STORE TO LAND, and then retrieves the same key THREE times.
#
# The wait is not optional. batched_put() is an async submit, so LMCache's
# "Stored ... put_time: 0.12 ms" is logged while the bytes are still in flight.
# Without the wait a retrieve can read a half-written object, and the
# classification below collapses: the first version of this script reported all
# 20 aliased failures as read-side, which is impossible for a store that wrote
# one fixed wrong payload. daos_store_quiesce.py polls until every visible
# object is complete (payload's last byte readable) and the set has stopped
# changing.
#
#   pass          all three retrieves equal the computed pass
#   store-side    all three agree with each other but differ from computed
#                 -- the object in DAOS is wrong, and wrong consistently
#   read-side     the three retrieves disagree with each other -- the object is
#                 known complete and cannot change, so the fault is after the
#                 read
#   unsettled     the store never quiesced, so nothing is claimed about it
#
# Prose, not a repeated word list: with a cycling vocabulary the top logits sit
# within noise and a harmless difference flips the token, which is how an
# earlier version of this check reported corruption on a configuration that was
# provably correct.
#
# Also counts LMCache's "Double free occurred somewhere" warnings per trial, to
# test whether failures track MemoryObj lifetime trouble. Correlation is not
# cause, but a clean split either way narrows the search.
set -euo pipefail

TRIALS=${1:-20}
MAXTOK=${2:-24}
URL=${3:-http://127.0.0.1:8001/v1/completions}
CONT=${CONT:-vllm-daos}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# A per-run nonce in every prompt, so pass A is a genuine miss. Without it the
# prompts repeat across runs, pass A hits a key left over from a previous run,
# and the "computed reference" is itself a cache read -- possibly of corrupt
# data. That silently invalidated several earlier measurements of this bug: the
# container object count stayed at 380 across a 20-trial run because every
# store was overwriting the same 23 paths.
RUN_ID=${RUN_ID:-$(date +%s)-$$}
echo "run_id=$RUN_ID trials=$TRIALS"

PARA="Distributed object storage separates metadata from bulk data so that clients can address a shard directly without consulting a central server on every request. In practice the metadata service still becomes a bottleneck when the working set is small and the request rate is high, because each lookup costs a round trip. "

mkprompt() {
	python3 - "$1" "$MAXTOK" "$TMP" "$PARA" "$RUN_ID" <<'PY'
import json, sys
k, maxtok, tmp, para, run = (sys.argv[1], int(sys.argv[2]), sys.argv[3],
                             sys.argv[4], sys.argv[5])
json.dump({"model": "qwen3", "prompt": f"rate{run}-{k} " + para * 90,
           "max_tokens": maxtok, "temperature": 0, "seed": 11},
          open(f"{tmp}/p.json", "w"))
PY
}
ask() { curl -s -m 300 -X POST "$URL" -H 'Content-Type: application/json' \
	-d @"$TMP/p.json" -o "$TMP/$1.json"; }
txt() { python3 -c "
import json,sys
print(json.dumps(json.load(open(sys.argv[1]))['choices'][0]['text']))" "$TMP/$1.json"; }
hits() { podman logs "$CONT" 2>&1 | grep -c 'Retrieved .* out of' || true; }
warns() { podman logs "$CONT" 2>&1 | grep -c 'Double free occurred somewhere' || true; }

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
QUIESCE=${QUIESCE:-$HERE/daos_store_quiesce.py}
POOL=${DAOS_TEST_POOL:-gdspool}
CONTAINER=${DAOS_TEST_CONT:-kvlmc}
# Run the quiesce check wherever the DAOS client lives. In the container case
# the caller sets QUIESCE_CMD to a podman exec wrapper.
quiesce() {
	if [ -n "${QUIESCE_CMD:-}" ]; then
		eval "$QUIESCE_CMD"
	else
		DAOS_TEST_POOL=$POOL DAOS_TEST_CONT=$CONTAINER \
			python3 "$QUIESCE" -q --timeout 90
	fi
}

pass=0; store_side=0; read_side=0; inconclusive=0; unsettled=0
fail_with_warn=0; fail_no_warn=0; pass_with_warn=0; pass_no_warn=0

printf '%-6s %-12s %-9s %s\n' trial outcome warn_delta note
for k in $(seq 1 "$TRIALS"); do
	mkprompt "$k"
	w0=$(warns); h0=$(hits)
	ask A
	# Pass A MUST be a miss, or the "computed" reference is itself a cache
	# read and the whole trial proves nothing.
	ha=$(hits)
	if [ "$ha" -ne "$h0" ]; then
		printf '%-6s %-12s %-9s %s\n' "$k" invalid - "pass A hit the cache"
		inconclusive=$((inconclusive + 1))
		continue
	fi
	if ! quiesce >/dev/null 2>&1; then
		printf '%-6s %-12s %-9s %s\n' "$k" unsettled - "store never quiesced"
		unsettled=$((unsettled + 1))
		continue
	fi
	ask B1; sleep 1
	ask B2; sleep 1
	ask B3; sleep 1
	h1=$(hits); w1=$(warns)
	dw=$((w1 - w0))

	a=$(txt A); b1=$(txt B1); b2=$(txt B2); b3=$(txt B3)

	# Exactly the three retrieves must have hit. Fewer means a retrieve was
	# recomputed instead of served; more means something else is running.
	if [ $((h1 - ha)) -ne 3 ]; then
		printf '%-6s %-12s %-9s %s\n' "$k" inconclusive "$dw" "$((h1-ha)) hits, want 3"
		inconclusive=$((inconclusive + 1))
		continue
	fi

	if [ "$a" = "$b1" ] && [ "$a" = "$b2" ] && [ "$a" = "$b3" ]; then
		outcome=pass; pass=$((pass + 1))
		[ "$dw" -gt 0 ] && pass_with_warn=$((pass_with_warn + 1)) || pass_no_warn=$((pass_no_warn + 1))
	elif [ "$b1" = "$b2" ] && [ "$b2" = "$b3" ]; then
		outcome=store-side; store_side=$((store_side + 1))
		[ "$dw" -gt 0 ] && fail_with_warn=$((fail_with_warn + 1)) || fail_no_warn=$((fail_no_warn + 1))
	else
		outcome=read-side; read_side=$((read_side + 1))
		[ "$dw" -gt 0 ] && fail_with_warn=$((fail_with_warn + 1)) || fail_no_warn=$((fail_no_warn + 1))
	fi
	printf '%-6s %-12s %-9s\n' "$k" "$outcome" "$dw"
done

n=$((pass + store_side + read_side))
fails=$((store_side + read_side))
echo
echo "trials=$n (inconclusive $inconclusive, unsettled $unsettled excluded)"
echo "  pass       $pass"
echo "  store-side $store_side   (retrieves agree with each other, differ from computed)"
echo "  read-side  $read_side   (retrieves disagree with each other)"
[ "$n" -gt 0 ] && python3 - "$fails" "$n" <<'PY'
import math, sys
f, n = int(sys.argv[1]), int(sys.argv[2])
p = f / n
# Wilson score interval: usable at small n and near 0 or 1, unlike the normal
# approximation, which is where this measurement lives.
z = 1.96
d = 1 + z*z/n
c = (p + z*z/(2*n)) / d
h = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
print(f"failure rate {p*100:.1f}%  95% CI [{max(0,c-h)*100:.1f}%, {min(1,c+h)*100:.1f}%]")
if f == 0:
    print("NOTE: zero failures does not mean fixed -- with n="
          f"{n} the interval still admits rates up to {min(1,c+h)*100:.1f}%.")
PY
echo
echo "double-free warnings vs outcome:"
printf '  fail with warnings %-4s  fail without %-4s\n' "$fail_with_warn" "$fail_no_warn"
printf '  pass with warnings %-4s  pass without %-4s\n' "$pass_with_warn" "$pass_no_warn"
