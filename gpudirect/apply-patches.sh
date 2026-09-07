#!/usr/bin/env bash
#
# Apply the GPU-direct fixes to a DAOS source tree and its bundled deps.
#
# Two stages, because the bundled UCX, Mercury and libfabric sources only
# exist after a first build pass has downloaded them:
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
#
# patch(1) also mis-reports the state once that series has shifted the
# surrounding lines: BOTH the forward and the reverse dry-run then fail even
# though the change is in, and the stage dies before it reaches the patches
# that come after it. Take a marker string as a third argument -- a token the
# patch introduces and nothing else uses -- and trust it over the dry-runs.
apply_plain() {
	local tree=$1 patch=$2 marker=${3:-} name
	name=$(basename "$patch")

	if [ -n "$marker" ] && grep -rqF -- "$marker" "$tree" 2>/dev/null; then
		say "$name: already applied (marker '$marker' present)"
		return 0
	fi
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
	OFI=$BUILD_ROOT/external/release/ofi
	echo "== bundled deps under: $BUILD_ROOT"
	for d in "$UCX" "$HG" "$OFI"; do
		[ -d "$d" ] || { echo "  $d not found -- run a build pass first" >&2; exit 1; }
	done

	apply_git "$UCX" "$PATCHES/ucx-0001-advertise-cuda-reg-via-dmabuf.patch"
	apply_plain "$HG" "$PATCHES/mercury-0001-keep-cuda-memtype-tls.patch" NA_UCX_EXTRA_TLS
	apply_git "$OFI" "$PATCHES/libfabric-0001-verbs-cuda-dmabuf-and-close-fd.patch"

	cat <<-'WARN'

	  ---------------------------------------------------------------------
	  All three patches exist to make dfs_read_gpu()/dfs_write_gpu() work.

	  The libfabric one is the safe one: it only affects registration of
	  device memory (FI_HMEM_CUDA), which the host path never asks for, and
	  it plugs an fd leak that would otherwise kill long GPU-direct runs.
	  It is also inert unless libfabric is configured --with-cuda; see
	  README.md, section "GDS over ofi+verbs", for that configure line.

	  The UCX and Mercury pair is NOT wanted by the production (host-memory)
	  path, and one of them used to break it.

	  The Mercury patch adds UCX memory-type components (cuda_copy,
	  cuda_ipc) to the TLS list. An earlier version did that by DEFAULT, and
	  on any client where CUDA is loadable that silently corrupted ordinary
	  host-memory bulk transfers: one 4 MiB region per transfer, only with 4
	  or more concurrent readers, sizes and return codes all normal. It is
	  now opt-in -- nothing happens unless NA_UCX_EXTRA_TLS names the
	  components.

	  So after building with these patches:
	    * leave NA_UCX_EXTRA_TLS unset/empty for anything that is not a
	      GPU-direct experiment
	    * if you do set it, re-run tests/test_rawio_integrity.py (28 MiB,
	      16 threads) and require a clean pass before trusting any result
	      measured with it -- the failure mode is silent

	  Note that gpudirect/PLAN.md records the decision not to build the
	  GPU-direct backend, so on current evidence there is no reason to
	  enable the CUDA components at all.
	  ---------------------------------------------------------------------
	WARN

	cat <<-EOF

	  Rebuild the components straight into PREFIX -- DAOS loads the prereq
	  .so by path, so it needs no relink:
	    (cd $UCX && make -j"\$(nproc)" && make install)
	    (cd $HG.build && make -j"\$(nproc)" && make install)
	    (cd $OFI && make -j"\$(nproc)" && make install)

	  libfabric only picks the dma-buf path up if it was configured with
	  CUDA, and plain --with-cuda=DIR is not enough (its -lcudart probe
	  fails). Reconfigure it once with:
	    CPPFLAGS=-I/usr/local/cuda/include \\
	    LDFLAGS="-L/usr/local/cuda/lib64 -L/usr/local/cuda/lib64/stubs" \\
	      ./configure --prefix=<prefix> ... --with-cuda=/usr/local/cuda \\
	      --enable-cuda-dlopen
	  and make sure unversioned libcudart.so / libcuda.so exist on the
	  runtime LD path -- libfabric dlopens those names.
	  Then re-apply the RPATH scons uses, or the modules will not find libucs:
	    patchelf --set-rpath '\$ORIGIN':<prefix>/prereq/release/ucx/lib64 \\
	        <prefix>/prereq/release/ucx/lib64/{*.so*,ucx/*.so*}

	  Do NOT run 'scons --build-deps=yes' after this. It does
	  'git reset --hard' on the prereq trees, which silently discards these
	  patches -- the build then succeeds and only fails at runtime, with
	  ucp_mem_map() returning -EINVAL again. If you must re-run it, re-run
	  this deps stage and the two rebuilds afterwards.
	EOF
	;;

*)
	usage
	;;
esac
