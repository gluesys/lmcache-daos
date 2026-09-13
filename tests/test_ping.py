# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""ping() against a live DAOS pool, and against a dead one.

The second half is the point. Until ping() existed, LMCache's health check
read support_ping() as False and returned healthy unconditionally, so a dead
backend and a cold cache were indistinguishable from the outside. A test that
only proved "ping returns 0 when things work" would not have caught that; it
has to show a non-zero code when DAOS is gone.

  test_ping.py <pool> <container>        # live only
  test_ping.py <pool> <container> --kill # also stop daos_server and re-probe
                                         # (single-node hosts only!)
"""
import asyncio
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = 0


def ck(what, ok, extra=""):
    global fails
    if not ok:
        fails += 1
    print("  %-50s %s %s" % (what, "PASS" if ok else "FAIL", extra))


def main():
    pool = sys.argv[1] if len(sys.argv) > 1 else "kvpool"
    cont = sys.argv[2] if len(sys.argv) > 2 else "nixltest"
    do_kill = "--kill" in sys.argv

    from lmcache_daos.connector import DaosConnector

    # The connector takes its config and metadata from a LocalCPUBackend, the
    # way LMCache's adapter constructs it, so build a real one rather than a
    # stub: a stub would not catch a change in what the connector reads from
    # it, and that is the kind of breakage this file exists to notice.
    from lmcache.v1.config import LMCacheEngineConfig
    from lmcache.v1.metadata import LMCacheMetadata
    from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend

    import torch

    cfg = LMCacheEngineConfig.from_defaults()
    cfg.remote_url = f"plugin://daos/{pool}/{cont}"
    cfg.local_cpu = False

    # Shape and dtype only decide how the staging pool is sized; ping() never
    # allocates from it. Keep them small so this runs anywhere.
    meta = LMCacheMetadata(
        model_name="ping-test",
        world_size=1,
        local_world_size=1,
        worker_id=0,
        local_worker_id=0,
        kv_dtype=torch.bfloat16,
        kv_shape=(1, 2, 256, 8, 128),
    )
    local = LocalCPUBackend(cfg, meta, dst_device="cpu")

    # The connector dispatches blocking DAOS calls with
    # loop.run_in_executor(), so it needs a real loop -- LMCache hands it the
    # engine's. Drive one here rather than asyncio.run() per call, which would
    # build and tear down a loop the connector never saw.
    loop = asyncio.new_event_loop()
    conn = DaosConnector(loop=loop, local_cpu_backend=local, config=cfg)

    ck("support_ping() is True", conn.support_ping() is True)

    rc = loop.run_until_complete(conn.ping())
    ck("ping() on a live pool returns 0", rc == 0, f"(rc={rc})")

    # Repeated pings must stay cheap and must not leave anything behind: the
    # health monitor calls this on a timer for the life of the process.
    rcs = [loop.run_until_complete(conn.ping()) for _ in range(20)]
    ck("20 consecutive pings all return 0", set(rcs) == {0}, f"(codes={sorted(set(rcs))})")

    if not do_kill:
        print("\n  (--kill 미지정: 장애 경로 미검증)")
        print("\n  === %s (%d failure%s) ===" % ("ALL PASS" if not fails else "FAILED",
                                                 fails, "" if fails == 1 else "s"))
        return 1 if fails else 0

    print("\n  -- stopping daos_server --")
    subprocess.run(["pkill", "-f", "daos_server"], check=False)
    subprocess.run(["sleep", "8"], check=False)

    import time
    from lmcache_daos.connector import PING_TIMEOUT_SECS

    t0 = time.perf_counter()
    rc = loop.run_until_complete(conn.ping())
    dt = time.perf_counter() - t0
    ck("ping() with DAOS down returns non-zero", rc != 0, f"(rc={rc})")

    # The point of the timeout. Without it DAOS retries internally and this
    # took over three minutes, by which time the answer is useless: the health
    # monitor polls every 30 s.
    ck("it gives up promptly", dt < PING_TIMEOUT_SECS + 2.0,
       f"({dt:.1f}s, limit {PING_TIMEOUT_SECS}s)")

    # A second probe must not queue behind the first, still-stuck one.
    t0 = time.perf_counter()
    rc2 = loop.run_until_complete(conn.ping())
    dt2 = time.perf_counter() - t0
    ck("a second ping returns at once, not after another timeout", dt2 < 1.0,
       f"({dt2:.2f}s, rc={rc2})")

    print("\n  === %s (%d failure%s) ===" % ("ALL PASS" if not fails else "FAILED",
                                            fails, "" if fails == 1 else "s"))
    print("  참고: daos_server 를 다시 올려야 합니다.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
