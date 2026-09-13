#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
#
# Drive tests/failure_modes.py through a fault matrix, in the one order the
# host allows.
#
# The ordering is forced, not stylistic. SCM on this host is a ramdisk, so
# stopping daos_server destroys the pool: every fault that leaves the pool
# usable has to run first, and the server-outage phases are terminal. That also
# limits what can be claimed -- see the RECOVERY note below.
#
# Each scenario runs in its OWN process. A fault that wedges a DAOS call leaves
# the thread stuck in the executor pool, uncancellable; sharing a process would
# let one scenario's damage decide the next scenario's result.
#
#   sudo bash tests/failure_modes.sh [pool] [container]
#
# Needs root (pkill, daos_server, systemctl) and a single-node DAOS it is
# allowed to destroy. DO NOT run this against a shared cluster.
set -u
POOL=${1:-${DAOS_TEST_POOL:-kvpool}}
CONT=${2:-${DAOS_TEST_CONT:-fmtest}}
PY=${PY:-/mnt/nvme1/venvs/lmcache-prof-src/bin/python}
REPO=${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
RESTORE=${RESTORE:-/root/restore_daos.sh}
KEYS=${KEYS:-8}
MIB=${MIB:-4}
LOG=${LOG:-/tmp/failure_modes.$$.log}

bad=0
phase() { printf '\n\033[1m### %s\033[0m\n' "$*"; }
run() {   # run <scenario> [extra args...]
    local sc=$1; shift
    "$PY" "$REPO/tests/failure_modes.py" "$POOL" "$CONT" --scenario "$sc" \
        --keys "$KEYS" --mib "$MIB" "$@" 2>&1 | grep -vE '^\s*$' | tee -a "$LOG"
    local rc=${PIPESTATUS[0]}
    bad=$((bad + rc))
    return 0
}
mkcont() { daos cont create "$POOL" "$CONT" --type POSIX >/dev/null 2>&1; }

echo "pool=$POOL cont=$CONT keys=$KEYS x ${MIB}MiB  log=$LOG"
mkcont

phase "1/5  baseline -- the harness can tell right from wrong"
run baseline

phase "2/5  daos_agent dies and comes back (the only truly reversible fault here)"
run agent-cycle

phase "3/5  the container is destroyed under an open handle"
run cont-destroy
mkcont

# Sized so the write is still running when the kill lands: 6x4 MiB finished in
# 0.03 s on the first run and the scenario was vacuous. The harness now says so
# rather than reporting a pass, but it still has to be given enough work to
# interrupt.
phase "4/5  SIGKILL mid-write"
KILL_KEYS=${KILL_KEYS:-64} KILL_MIB=${KILL_MIB:-8}
"$PY" "$REPO/tests/failure_modes.py" "$POOL" "$CONT" --scenario kill-mid-put \
    --keys "$KILL_KEYS" --mib "$KILL_MIB" --kill-after "${KILL_AFTER:-0.2}" 2>&1 \
    | grep -vE '^\s*$' | tee -a "$LOG"
bad=$((bad + PIPESTATUS[0]))

phase "5/5  the pool is simply gone"
pkill -9 -f daos_server 2>/dev/null; sleep 3
run put
run get

# RECOVERY after a server outage is deliberately NOT asserted here. Restoring
# this host means reformatting, so the pool that comes back has a new UUID; a
# connector that had been permanently broken by the outage and one that is
# correctly rejecting a stale handle would look identical. Answering it needs a
# pool that survives a restart -- i.e. MD-on-SSD.
phase "restore"
if [ -x "$RESTORE" ]; then bash "$RESTORE" 2>&1 | tail -4; else echo "no $RESTORE -- restore by hand"; fi

printf '\n\033[1m=== %d finding%s (WRONG/HANG/STUCK) -- full log %s ===\033[0m\n' \
    "$bad" "$([ "$bad" = 1 ] || echo s)" "$LOG"
exit $((bad > 0))
