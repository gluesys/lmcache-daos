#!/usr/bin/env bash
#
# Build tests/spdk_blob_tagio.c against the SPDK build tree that DAOS bundles.
#
# Separate script because the link line is long and order-sensitive: the app
# framework pulls in the bdev/accel/event stack, and everything has to be
# inside --whole-archive so SPDK's constructor-registered modules (bdev_nvme,
# accel drivers, the event subsystems) actually land in the binary. Omitting
# any of the event_* or bdev_* archives yields a binary that starts and then
# reports "could not open bdev".
#
#   ./spdk_blob_tagio.build.sh [source.c] [output]
set -eu

SRC=${1:-/tmp/spdk_blob_tagio.c}
OUT=${2:-/tmp/spdk_blob_tagio}
B=${SPDK_TREE:-/var/daosbuild/build-stockfull/external/release/spdk}
D=$B/dpdk/build/lib
ISAL=${ISAL_LIB:-/var/daos-stockfull/prereq/release/isal/lib64}
# blobstore pulls in accel's compress/crypto paths, which need isal_crypto and lz4
ISALC=${ISALC_LIB:-/var/daos-stockfull/prereq/release/isal_crypto/lib64}

spdk_libs=""
for l in blob blob_bdev bdev bdev_nvme bdev_aio bdev_malloc nvme event event_bdev \
         event_accel event_iobuf event_sock event_vmd event_keyring event_scheduler \
         accel accel_error thread util log json jsonrpc rpc sock trace notify \
         env_dpdk dma keyring vfio_user vmd init ftl_bdev; do
	[ -f "$B/build/lib/libspdk_$l.a" ] && spdk_libs="$spdk_libs $B/build/lib/libspdk_$l.a"
done

rte_libs=""
for l in eal ring mempool mbuf pci bus_pci bus_vdev kvargs telemetry log \
         mempool_ring net ethdev meter cmdline hash rcu dmadev power vhost; do
	[ -f "$D/librte_$l.a" ] && rte_libs="$rte_libs -lrte_$l"
done

set -x
gcc -O2 -pthread -o "$OUT" "$SRC" \
    -I"$B/include" -I"$B/dpdk/build/include" \
    -Wl,--whole-archive $spdk_libs -Wl,--no-whole-archive \
    -L"$D" -Wl,--whole-archive $rte_libs -Wl,--no-whole-archive \
    -L"$ISAL" -lisal -L"$ISALC" -lisal_crypto -llz4 \
    -lnuma -ldl -lrt -luuid -lssl -lcrypto -lm -laio
