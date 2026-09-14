#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Does a cache hit produce the same tokens as computing from scratch?
#
# This exists because the interesting configurations of this stack are ones
# where the KV cache could come back subtly wrong rather than fail loudly --
# LMCache 0.5.2's official wheel needs libcudart.so.13 while the container's
# torch is cu128, so making the fused c_ops kernels load at all means running
# two CUDA runtimes in one process. A wrong-but-plausible KV tensor is the
# worst failure mode available here: generation stays fluent and nothing logs
# an error, so a speed measurement alone would happily report a large win on a
# broken cache.
#
#   ./kv_correctness_gate.sh [url] [n_prompts] [max_tokens]
#
# Method: for each distinct prompt, send it twice at temperature 0.
#   pass A -- nothing cached, vLLM computes the prefill, LMCache stores it
#   pass B -- LMCache reports a hit and restores the KV instead of computing
# At temperature 0 with the same prompt, B must emit the same tokens as A. If
# the restored KV is wrong, B diverges.
#
# Interpretation, stated plainly: matching output is evidence, not proof --
# a KV error small enough not to change the argmax would pass. Divergence is
# a hard fail. Run it against the known-good configuration first so you know
# the baseline passes; only then compare a new one.
#
# Also verifies pass B was actually served from cache. Without that check a
# "pass" could just mean the cache was never consulted, which is how this kind
# of gate usually ends up green and meaningless.
set -euo pipefail

URL=${1:-http://127.0.0.1:8001/v1/completions}
N=${2:-6}
MAXTOK=${3:-24}
CONT=${CONT:-vllm-daos}
# How to read the serving process's log. The default assumes the podman
# deployment this was written against; a bare venv run needs
#   LOG_CMD="cat /path/to/vllm.log"
# Kept as a command rather than a path so both shapes work with one variable.
LOG_CMD=${LOG_CMD:-podman logs $CONT}
# Log line that proves pass B was served from cache. In-process mode logs
# "Retrieved N out of M"; MP mode's server logs "Retrieved N tokens in T seconds"
# (prefixed by the launcher). Override with HIT_PATTERN for other setups.
HIT_PATTERN=${HIT_PATTERN:-'Retrieved [0-9]+ (out of|tokens in)'}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# The served model name and the prompt length were fixed for one deployment.
# They have to move together with the model: a name vLLM does not serve gets a
# 404 with no "choices", and a prompt past the context window gets a 400, and
# both arrive looking like the backend failed.
GATE_MODEL=${GATE_MODEL:-qwen3}
PARA_REPEAT=${PARA_REPEAT:-90}     # ~6000 tokens; ~65 tokens per repeat
python3 - "$N" "$MAXTOK" "$TMP" "$GATE_MODEL" "$PARA_REPEAT" <<'PY'
import json, sys
n, maxtok, tmp = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
model, repeat = sys.argv[4], int(sys.argv[5])

# Coherent prose, NOT a repeated word list. The first version of this gate used
# " ".join of a cycling 8-word vocabulary, and that is the worst possible prompt
# for an argmax-equality test: with the same few tokens repeating, the top
# candidates sit within noise of each other, so a difference far too small to
# matter flips the sampled token and the gate reports corruption that is not
# there. It scored 2 of 4 on a configuration whose retrieves were provably
# self-consistent and identical to the computed pass on prose. Prose keeps a
# margin between the top logits, so a flip means the KV really changed.
para = ("Distributed object storage separates metadata from bulk data so that "
        "clients can address a shard directly without consulting a central "
        "server on every request. In practice the metadata service still "
        "becomes a bottleneck when the working set is small and the request "
        "rate is high, because each lookup costs a round trip. ")
for k in range(n):
    # ~6000 tokens so the prompt spans many chunks; unique prefix per k so
    # pass A is always a genuine miss.
    txt = f"gate{k} " + (para * repeat)
    json.dump({"model": model, "prompt": txt, "max_tokens": maxtok,
               "temperature": 0, "seed": 1234},
              open(f"{tmp}/g{k}.json", "w"))
PY

get_text() {  # $1=file -> completion text, newline-escaped
	python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
print(json.dumps(d['choices'][0]['text']))
" "$1"
}

pass=0
fail=0
nocache=0
for k in $(seq 0 $((N - 1))); do
	mark_before=$($LOG_CMD 2>&1 | grep -Ec "$HIT_PATTERN" || true)

	curl -s -m 300 -X POST "$URL" -H 'Content-Type: application/json' \
		-d @"$TMP/g$k.json" -o "$TMP/a$k.json"
	sleep 2
	curl -s -m 300 -X POST "$URL" -H 'Content-Type: application/json' \
		-d @"$TMP/g$k.json" -o "$TMP/b$k.json"
	sleep 1

	a=$(get_text "$TMP/a$k.json")
	b=$(get_text "$TMP/b$k.json")
	mark_after=$($LOG_CMD 2>&1 | grep -Ec "$HIT_PATTERN" || true)

	if [ "$mark_after" -le "$mark_before" ]; then
		# Pass B never hit the cache, so this iteration proves nothing.
		echo "prompt $k: INCONCLUSIVE -- no cache hit logged"
		nocache=$((nocache + 1))
		continue
	fi
	if [ "$a" = "$b" ]; then
		echo "prompt $k: match"
		pass=$((pass + 1))
	else
		echo "prompt $k: MISMATCH"
		echo "  computed: $a"
		echo "  restored: $b"
		fail=$((fail + 1))
	fi
done

echo
echo "match=$pass mismatch=$fail inconclusive=$nocache"
if [ "$fail" -gt 0 ]; then
	echo "GATE: FAIL -- restored KV changes generation. Do not use this config."
	exit 1
fi
if [ "$pass" -eq 0 ]; then
	echo "GATE: INCONCLUSIVE -- nothing was ever served from cache."
	exit 2
fi
echo "GATE: PASS ($pass prompts served from cache, all identical)"
