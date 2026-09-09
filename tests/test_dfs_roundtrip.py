# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Integration test against a real DAOS runtime.

Skipped automatically unless DAOS_TEST_POOL and DAOS_TEST_CONT are set AND the
DAOS client libraries are importable. Run on the E2E runtime box:

    daos cont create <pool> <cont> --type POSIX
    DAOS_TEST_POOL=<pool> DAOS_TEST_CONT=<cont> python3 -m pytest tests/test_dfs_roundtrip.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POOL = os.environ.get("DAOS_TEST_POOL")
CONT = os.environ.get("DAOS_TEST_CONT")


def _skip(msg):
    print(f"SKIP: {msg}")
    sys.exit(0)


def main():
    if not (POOL and CONT):
        _skip("set DAOS_TEST_POOL / DAOS_TEST_CONT to run")
    try:
        from lmcache_daos.dfs_binding import DfsSys
    except OSError as e:
        _skip(f"DAOS client libs not loadable: {e}")

    dfs = DfsSys(pool=POOL, cont=CONT)
    try:
        path = "/lmcache_daos_selftest"
        payload = os.urandom(1 << 20)  # 1 MiB
        dfs.write(path, payload)
        assert dfs.exists(path), "file should exist after write"
        got = dfs.read(path, 0, len(payload))
        assert got == payload, f"roundtrip mismatch: {len(got)} vs {len(payload)}"
        assert dfs.remove(path) is True
        assert not dfs.exists(path), "file should be gone after remove"
        print("PASS dfs roundtrip (1 MiB write/read/remove)")
    finally:
        dfs.close()


if __name__ == "__main__":
    main()
