#!/usr/bin/env bash
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
# Each trial uses a fresh prose prompt (so pass A is a genuine miss) and then
# retrieves the same key THREE times. Three retrieves is what makes the result
# diagnostic rather than just a count:
#
#   pass          all three retrieves equal the computed pass
#   store-side    all three agree with each other but differ from computed
#                 -- the object in DAOS is wrong, and it is wrong consistently
#   read-side     the three retrieves disagree with each other -- the object
#                 cannot be changing, so the fault is after the read
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

PARA="Distributed object storage separates metadata from bulk data so that clients can address a shard directly without consulting a central server on every request. In practice the metadata service still becomes a bottleneck when the working set is small and the request rate is high, because each lookup costs a round trip. "

mkprompt() {
	python3 - "$1" "$MAXTOK" "$TMP" "$PARA" <<'PY'
import json, sys
k, maxtok, tmp, para = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
json.dump({"model": "qwen3", "prompt": f"rate{k} " + para * 90,
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

pass=0; store_side=0; read_side=0; inconclusive=0
fail_with_warn=0; fail_no_warn=0; pass_with_warn=0; pass_no_warn=0

printf '%-6s %-12s %-9s %s\n' trial outcome warn_delta note
for k in $(seq 1 "$TRIALS"); do
	mkprompt "$k"
	w0=$(warns); h0=$(hits)
	ask A; sleep 1
	ask B1; sleep 1
	ask B2; sleep 1
	ask B3; sleep 1
	h1=$(hits); w1=$(warns)
	dw=$((w1 - w0))

	a=$(txt A); b1=$(txt B1); b2=$(txt B2); b3=$(txt B3)

	# Three retrieves must have produced at least three hit lines, or the
	# trial says nothing about the cache.
	if [ $((h1 - h0)) -lt 3 ]; then
		printf '%-6s %-12s %-9s %s\n' "$k" inconclusive "$dw" "only $((h1-h0)) hits"
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
echo "trials=$n (inconclusive $inconclusive excluded)"
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
