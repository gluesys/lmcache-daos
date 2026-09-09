# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Wait until LMCache's stores have actually landed in DAOS.

Needed because ``batched_put()`` is an async submit: LMCache logs
"Stored N out of N tokens ... put_time: 0.12 ms" and returns long before the
bytes are in the container. Any test that stores and then immediately retrieves
is racing the writer, and that invalidated the store-side/read-side
classification in kv_failure_rate.sh -- all 20 aliased failures came back
labelled read-side, which cannot be right for a store that wrote one fixed
wrong payload.

Completeness is checked per object rather than by counting objects, because an
object becomes visible at open_rdwr_create, before its payload is written. For
each object this reads the 8-byte prefix for (meta_len, payload_len) and then
reads the payload's LAST byte. A short read there means the write has not
landed yet -- the same torn-object condition tests/test_torn_object.py covers.

    DAOS_TEST_POOL=gdspool DAOS_TEST_CONT=kvlmc \\
        python3 tests/daos_store_quiesce.py [--timeout S] [--stable K] [-q]

Exits 0 once every visible object is complete and the set has stopped changing
for K consecutive polls; exits 1 on timeout. Requiring stability as well as
completeness matters: a store of 23 chunks passes through many moments where
everything written so far is complete and more is still coming.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lmcache_daos import serde  # noqa: E402
from lmcache_daos.dfs_binding import DaosError, DfsSys  # noqa: E402


def survey(dfs: DfsSys):
    """Return (n_objects, n_complete, n_incomplete, total_payload_bytes)."""
    n = complete = incomplete = 0
    total = 0
    ps = serde.prefix_size()
    for name in dfs.iterdir("/"):
        path = "/" + name
        n += 1
        try:
            obj = dfs.open_rdonly(path)
        except DaosError:
            # Vanished or not yet openable; treat as still in flight.
            incomplete += 1
            continue
        try:
            hdr = dfs.read_obj(obj, 0, ps)
            if len(hdr) < ps:
                incomplete += 1
                continue
            meta_len, payload_len = serde.parse_prefix(hdr)
            if payload_len == 0:
                complete += 1
                continue
            end = ps + meta_len + payload_len
            tail = dfs.read_obj(obj, end - 1, 1)
            if len(tail) == 1:
                complete += 1
                total += payload_len
            else:
                incomplete += 1
        except DaosError:
            incomplete += 1
        finally:
            try:
                dfs.close_obj(obj)
            except Exception:
                pass
    return n, complete, incomplete, total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--stable", type=int, default=3)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args()

    dfs = DfsSys(pool=os.environ.get("DAOS_TEST_POOL", "gdspool"),
                 cont=os.environ.get("DAOS_TEST_CONT", "kvlmc"))
    deadline = time.monotonic() + a.timeout
    prev = None
    stable = 0

    while time.monotonic() < deadline:
        n, comp, inc, total = survey(dfs)
        cur = (n, comp, total)
        if inc == 0 and cur == prev:
            stable += 1
            if stable >= a.stable:
                if not a.quiet:
                    print(f"objects={n} complete={comp} incomplete=0 "
                          f"payload_bytes={total} status=quiesced")
                return 0
        else:
            stable = 0
        prev = cur
        time.sleep(a.interval)

    n, comp, inc, total = survey(dfs)
    print(f"objects={n} complete={comp} incomplete={inc} "
          f"payload_bytes={total} status=timeout", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
