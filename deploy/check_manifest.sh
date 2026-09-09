#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Verify the deployed DAOS builds against deploy/MANIFEST.md.
#
#   ./check_manifest.sh [host ...]        # default: the hosts in EXPECTED below
#
# A manifest nobody checks is decoration, and the failure this guards against
# was invisible precisely because nothing compared intent to reality: the
# servers were rebuilt for the GPU-direct work while the vLLM container kept
# mounting a client bundle from five days earlier, and concurrent multi-chunk
# reads silently returned one wrong 4 MiB region.
#
# Identity is (libdaos size, HG_Bulk_import_rkey present) because the FILE NAME
# carries no version -- DAOS installs libdaos.so.2.8.0 whatever it built. The
# symbol is the stub gpudirect/patches/daos-0004 adds, so it doubles as "were
# the GPU-direct patches applied to this build".
#
# Exits non-zero on any mismatch or unreachable host, so it can gate a deploy.
set -uo pipefail

# host:path:libdaos_size:rkey_stub(0|1)
EXPECTED=(
	"cell1:/opt/daos-gds:8950504:1"
	"cell1:/opt/daos-gds-gpu:8950616:1"
	"cell2:/opt/daos-gds:8950504:1"
	"client-5:/opt/daos-gds-gpu:8950616:1"
	"client-6:/opt/daos-gds-gpu:8950616:1"
	"client-6:/root/daoslibs29:8950616:1"
)

# Bundles that must NOT be mounted into a serving container. Recorded so a
# regression is caught, not so they are deleted -- they are useful controls.
FORBIDDEN_BUNDLE="/root/daoslibs"

probe() { # $1=host $2=path -> "size rkey"
	local h=$1 p=$2 cmd
	cmd='
p="'"$p"'"
f=$(ls $p/lib64/libdaos.so.2.* 2>/dev/null | head -1)
[ -n "$f" ] || f=$(ls $p/libdaos.so.2.* 2>/dev/null | head -1)
[ -n "$f" ] || { echo "MISSING 0"; exit 0; }
hg=$(ls $p/prereq/release/mercury/lib64/libmercury.so.2.* 2>/dev/null | head -1)
[ -n "$hg" ] || hg=$(ls $p/libmercury.so.2 2>/dev/null | head -1)
r=0
[ -n "$hg" ] && r=$(nm -D --defined-only "$hg" 2>/dev/null | grep -c HG_Bulk_import_rkey)
[ "$r" -gt 1 ] && r=1
echo "$(stat -c%s "$f") $r"
'
	if [ "$h" = "$(hostname -s)" ] || [ "$h" = local ]; then
		bash -c "$cmd"
	else
		ssh -o ConnectTimeout=8 -o BatchMode=yes "$h" "bash -c '$cmd'" 2>/dev/null
	fi
}

fail=0
printf '%-9s %-24s %-10s %-10s %-6s %s\n' HOST PATH SIZE WANT RKEY RESULT
for e in "${EXPECTED[@]}"; do
	IFS=: read -r h p want_sz want_rk <<<"$e"
	out=$(probe "$h" "$p")
	if [ -z "$out" ]; then
		printf '%-9s %-24s %-10s %-10s %-6s %s\n' "$h" "$p" - "$want_sz" - "UNREACHABLE"
		fail=1
		continue
	fi
	read -r sz rk <<<"$out"
	if [ "$sz" = "$want_sz" ] && [ "$rk" = "$want_rk" ]; then
		res=ok
	else
		res="MISMATCH (rkey want $want_rk got $rk)"
		fail=1
	fi
	printf '%-9s %-24s %-10s %-10s %-6s %s\n' "$h" "$p" "$sz" "$want_sz" "$rk" "$res"
done

echo
echo "container bundle actually mounted (must not be $FORBIDDEN_BUNDLE):"
for h in client-5 client-6; do
	m=$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$h" \
		"podman inspect vllm-daos --format '{{range .Mounts}}{{if eq .Destination \"/daoslib\"}}{{.Source}}{{end}}{{end}}' 2>/dev/null" 2>/dev/null)
	if [ -z "$m" ]; then
		printf '  %-9s no vllm-daos container\n' "$h"
	elif [ "$m" = "$FORBIDDEN_BUNDLE" ]; then
		printf '  %-9s %s  <-- FORBIDDEN, this is the mismatched bundle\n' "$h" "$m"
		fail=1
	else
		printf '  %-9s %s  ok\n' "$h" "$m"
	fi
done

echo
echo "NA_UCX_EXTRA_TLS must be present and empty (see gpudirect/patches/mercury-0001):"
for h in client-5 client-6; do
	v=$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$h" \
		"podman inspect vllm-daos --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^NA_UCX_EXTRA_TLS=' || true" 2>/dev/null)
	if [ -z "$v" ]; then
		printf '  %-9s not set\n' "$h"
	elif [ "$v" = "NA_UCX_EXTRA_TLS=" ]; then
		printf '  %-9s empty  ok\n' "$h"
	else
		printf '  %-9s %s  <-- CUDA components enabled; host-memory transfers are unsafe\n' "$h" "$v"
		fail=1
	fi
done

echo
if [ "$fail" -eq 0 ]; then
	echo "MANIFEST OK -- but this only checks build identity. Correctness still"
	echo "needs tests/test_rawio_integrity.py (28 16 5) and kv_correctness_gate.sh."
else
	echo "MANIFEST MISMATCH -- do not serve from this deployment until resolved."
fi
exit "$fail"
