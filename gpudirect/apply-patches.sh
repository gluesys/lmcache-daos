#!/usr/bin/env bash
#
# Apply the GPU-direct fixes to a DAOS source tree and its bundled deps.
#
# Two stages, because UCX and Mercury sources only exist after a first build
# pass has downloaded them:
#
#   ./apply-patches.sh daos <daos-src> [<build-root>]
#   ./apply-patches.sh deps <daos-src> <build-root>
#
# See README.md for the full build order. Every apply is checked before it is
# performed and the script stops on the first failure -- a half-patched tree
# builds into something that looks fine and fails at runtime.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PATCHES=$HERE/patches

usage() {
	echo "usage: $0 {daos|deps} <daos-src> [<build-root>]" >&2
	exit 2
}

[ $# -ge 2 ] || usage
STAGE=$1
SRC=$(cd "$2" && pwd) || usage
BUILD_ROOT=${3:-$SRC/build}

say() { printf '  %s\n' "$*"; }

# git apply, but refuse to do anything unless the whole patch applies. Skips
# cleanly if it is already in (so the script is safe to re-run).
apply_git() {
	local tree=$1 patch=$2 name
	name=$(basename "$patch")

	if git -C "$tree" apply --reverse --check "$patch" 2>/dev/null; then
		say "$name: already applied"
		return 0
	fi
	if ! git -C "$tree" apply --check "$patch" 2>/dev/null; then
		echo "  $name: DOES NOT APPLY to $tree" >&2
		echo "  (upstream moved, or the tree is at a different commit)" >&2
		return 1
	fi
	git -C "$tree" apply "$patch"
	say "$name: applied"
}

# Mercury's tree already carries DAOS's own patch series as uncommitted
# changes, so a git-based check would see them too. Use plain patch(1) there.
apply_plain() {
	local tree=$1 patch=$2 name
	name=$(basename "$patch")

	if patch -d "$tree" -p1 --dry-run --reverse --force <"$patch" >/dev/null 2>&1; then
		say "$name: already applied"
		return 0
	fi
	if ! patch -d "$tree" -p1 --dry-run <"$patch" >/dev/null 2>&1; then
		echo "  $name: DOES NOT APPLY to $tree" >&2
		return 1
	fi
	patch -d "$tree" -p1 <"$patch" >/dev/null
	say "$name: applied"
}

case $STAGE in
daos)
	echo "== DAOS tree: $SRC"
	[ -f "$SRC/SConstruct" ] || { echo "  not a DAOS source tree" >&2; exit 1; }

	for p in daos-0001-gate-cufile-sconscript \
		 daos-0002-enlarge-tse-task-arg-len \
		 daos-0003-ucx-explicit-cuda-paths \
		 daos-0004-replace-broken-rkey-patch; do
		apply_git "$SRC" "$PATCHES/$p.patch"
	done

	# The raft submodule is not optional: without it scons stops with
	# "missing SConscript file .../src/rdb/raft/SConscript".
	if [ ! -f "$SRC/src/rdb/raft/SConscript" ]; then
		say "raft submodule missing -- initialising"
		git -C "$SRC" submodule update --init --recursive
	else
		say "raft submodule: present"
	fi

	# scons copies deps/patches/* into the build root and reuses the copy,
	# so replacing 0006 upstream has no effect until the copy is gone.
	if [ -d "$BUILD_ROOT/external/release" ]; then
		rm -f "$BUILD_ROOT"/external/release/mercury__*
		say "cleared stale mercury patch copies in $BUILD_ROOT"
	fi
	;;

deps)
	[ $# -ge 3 ] || { echo "  deps stage needs <build-root>" >&2; usage; }
	UCX=$BUILD_ROOT/external/release/ucx
	HG=$BUILD_ROOT/external/release/mercury
	echo "== bundled deps under: $BUILD_ROOT"
	for d in "$UCX" "$HG"; do
		[ -d "$d" ] || { echo "  $d not found -- run a build pass first" >&2; exit 1; }
	done

	apply_git "$UCX" "$PATCHES/ucx-0001-advertise-cuda-reg-via-dmabuf.patch"
	apply_plain "$HG" "$PATCHES/mercury-0001-keep-cuda-memtype-tls.patch"

	cat <<-EOF

	  Rebuild both components straight into PREFIX -- DAOS loads the prereq
	  .so by path, so it needs no relink:
	    (cd $UCX && make -j"\$(nproc)" && make install)
	    (cd $HG.build && make -j"\$(nproc)" && make install)
	  Then re-apply the RPATH scons uses, or the modules will not find libucs:
	    patchelf --set-rpath '\$ORIGIN':<prefix>/prereq/release/ucx/lib64 \\
	        <prefix>/prereq/release/ucx/lib64/{*.so*,ucx/*.so*}

	  Do NOT run 'scons --build-deps=yes' after this. It does
	  'git reset --hard' on the prereq trees, which silently discards both
	  patches -- the build then succeeds and only fails at runtime, with
	  ucp_mem_map() returning -EINVAL again. If you must re-run it, re-run
	  this deps stage and the two rebuilds afterwards.
	EOF
	;;

*)
	usage
	;;
esac
