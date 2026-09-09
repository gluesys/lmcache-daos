#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Rebuild the container's DAOS client bundle from a chosen DAOS install.
#
#   ./mk_daoslibs_bundle.sh <daos-prefix> <out-dir> [template-dir]
#
# Why this exists: the vLLM container gets its DAOS client from a curated
# directory bind-mounted at /daoslib, and the copy in use was built from a
# different DAOS than the servers run. That pairing silently corrupts data --
# concurrent multi-chunk reads come back with one 4 MiB region wrong, sizes and
# return codes all normal. See gpudirect/README.md. dmg refuses a mismatched
# control plane outright; the data plane does not, so nothing warns you.
#
# The DAOS build system names the library libdaos.so.2.8.0 regardless of the
# actual version, so you cannot tell builds apart by filename -- only by
# content. That is exactly why the wrong bundle went unnoticed. Compare sizes:
# the stock 2.8 RPM build is ~2.1 MB, the 2.9.100 source build ~8.9 MB.
#
# It starts from an existing known-working bundle so the curation is kept (the
# rdma-core bits the container image lacks, daos.conf) and then, in two passes,
# replaces every file the template already had and adds any shared library the
# template lacked. The second pass is not optional: a newer DAOS introduces
# libraries the old bundle never contained -- 2.9.100 pulls in
# libdaos_mgmt_crtproto.so -- and replacing only known names produces a bundle
# that fails at dlopen with a missing-file error.
set -euo pipefail

PREFIX=${1:?usage: $0 <daos-prefix> <out-dir> [template-dir]}
OUT=${2:?usage: $0 <daos-prefix> <out-dir> [template-dir]}
TEMPLATE=${3:-/root/daoslibs}

[ -d "$PREFIX/lib64" ] || { echo "no $PREFIX/lib64" >&2; exit 1; }
[ -d "$TEMPLATE" ]     || { echo "no template $TEMPLATE" >&2; exit 1; }
[ -e "$OUT" ] && { echo "$OUT exists -- refusing to overwrite" >&2; exit 1; }

echo "template : $TEMPLATE"
echo "source   : $PREFIX"
echo "output   : $OUT"
cp -a "$TEMPLATE" "$OUT"

replaced=0
added=0
missing=0
stale=0

# Find a library by name anywhere in the source install: DAOS's own lib64
# first, then the prereq trees. The fallback matters because the template
# keeps some prereq libraries at its top level as well as under mercury/ and
# ucx/. Updating only the subdirectory leaves a stale copy at the top level,
# and since LD_LIBRARY_PATH lists the top level first, the loader picks the
# stale one -- which surfaces as
#   libcart.so.4: undefined symbol: HG_Bulk_import_rkey
# i.e. as a missing patch rather than as a shadowed library.
find_src() {
	local name=$1 d
	if [ -e "$PREFIX/lib64/$name" ]; then
		printf '%s\n' "$PREFIX/lib64/$name"
		return 0
	fi
	for d in "$PREFIX"/prereq/release/*/lib64; do
		if [ -e "$d/$name" ]; then
			printf '%s\n' "$d/$name"
			return 0
		fi
	done
	return 1
}

# Pass 1 -- top level: replace what the template already had.
while IFS= read -r name; do
	if src=$(find_src "$name"); then
		cp -aP --remove-destination "$src" "$OUT/$name"
		replaced=$((replaced + 1))
	else
		# Not provided by this DAOS install at all: the template's copy
		# stays, which is right for rdma-core bits but worth naming.
		stale=$((stale + 1))
	fi
done < <(cd "$TEMPLATE" && find . -maxdepth 1 -name 'lib*' -printf '%f\n')

# Pass 2 -- top level: add shared libraries the template did not have. A newer
# DAOS brings new ones, and without these dlopen fails outright.
while IFS= read -r name; do
	[ -e "$OUT/$name" ] && continue
	cp -aP "$PREFIX/lib64/$name" "$OUT/$name"
	added=$((added + 1))
done < <(cd "$PREFIX/lib64" && find . -maxdepth 1 -name 'lib*.so*' -printf '%f\n')

# Subdirectories map onto prereq trees, but NOT uniformly -- the mapping has to
# be stated per directory:
#
#   mercury/  holds Mercury's own libraries and its na plugins, and upstream
#             those sit together in prereq/release/mercury/lib64
#   ucx/      holds UCX's transport MODULES, which upstream live one level
#             deeper, in prereq/release/ucx/lib64/ucx. UCX's core libraries
#             (libucp/libucs/libuct) are at the bundle's TOP level and are
#             picked up by pass 1 via find_src.
#
# Getting this wrong is not loud. Pointing ucx/ at the core lib directory fills
# it with the wrong files and copies none of the 20 CUDA transport modules, and
# the resulting bundle still loads and still serves I/O -- it just behaves
# differently from the install it was built from.
declare -A SUBSRC=(
	[mercury]="$PREFIX/prereq/release/mercury/lib64"
	[ucx]="$PREFIX/prereq/release/ucx/lib64/ucx"
)
for sub in "${!SUBSRC[@]}"; do
	[ -d "$TEMPLATE/$sub" ] || continue
	psrc=${SUBSRC[$sub]}
	if [ ! -d "$psrc" ]; then
		echo "  WARN: no $psrc -- leaving $sub from template" >&2
		missing=$((missing + 1))
		continue
	fi
	while IFS= read -r name; do
		[ -e "$psrc/$name" ] || continue
		cp -aP --remove-destination "$psrc/$name" "$OUT/$sub/$name"
		replaced=$((replaced + 1))
	done < <(cd "$TEMPLATE/$sub" && find . -maxdepth 1 -name 'lib*' -printf '%f\n')
	while IFS= read -r name; do
		[ -e "$OUT/$sub/$name" ] && continue
		cp -aP "$psrc/$name" "$OUT/$sub/$name"
		added=$((added + 1))
	done < <(cd "$psrc" && find . -maxdepth 1 -name 'lib*.so*' -printf '%f\n')
done

echo "replaced $replaced, added $added, kept-from-template $stale file(s);" \
     "$missing prereq tree(s) left from template"

# The whole point of the bundle is that the client matches the servers, so
# check the one symbol that proves the patched Mercury made it in, at every
# path the loader could pick it up from. A shadowed stale copy is the failure
# this catches.
echo
echo "patched-Mercury check (HG_Bulk_import_rkey must be present in each):"
ok=1
for f in "$OUT"/libmercury.so.2* "$OUT"/mercury/libmercury.so.2*; do
	[ -f "$f" ] || continue
	n=$(nm -D --defined-only "$f" 2>/dev/null | grep -c HG_Bulk_import_rkey || true)
	printf '  %-52s %s\n' "${f#"$OUT"/}" "$([ "$n" -gt 0 ] && echo present || { echo MISSING; ok=0; })"
done
[ "$ok" = 1 ] || { echo "  -> bundle would fail at dlopen; not usable" >&2; exit 1; }

# Same idea for UCX: compare the module set against the source install rather
# than just checking the directory is non-empty. A bundle missing transport
# modules still works, which is why this has to be counted and shown.
src_ucx="$PREFIX/prereq/release/ucx/lib64/ucx"
if [ -d "$src_ucx" ] && [ -d "$OUT/ucx" ]; then
	printf '\nUCX transport modules: bundle %s vs source %s'\
' (cuda modules: %s vs %s)\n' \
		"$(find "$OUT/ucx" -maxdepth 1 -name '*.so*' | wc -l)" \
		"$(find "$src_ucx" -maxdepth 1 -name '*.so*' | wc -l)" \
		"$(find "$OUT/ucx" -maxdepth 1 -name '*cuda*' | wc -l)" \
		"$(find "$src_ucx" -maxdepth 1 -name '*cuda*' | wc -l)"
fi
echo
echo "sanity -- libdaos size should match the source build, not the template:"
for d in "$TEMPLATE" "$OUT" "$PREFIX/lib64"; do
	f=$(ls "$d"/libdaos.so.2.* 2>/dev/null | head -1) || true
	[ -n "${f:-}" ] && printf '  %-28s %10s  %s\n' \
		"$(basename "$d")" "$(stat -c%s "$f")" "$(basename "$f")"
done
cat <<-EOF

	Next: mount this at /daoslib instead of the old bundle, then PROVE it
	before trusting it -- the failure mode is silent:
	  DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> \\
	      python3 tests/test_rawio_integrity.py 28 16 5 hdr
	  ./tests/kv_correctness_gate.sh
EOF
