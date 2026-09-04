"""LMCache MP cache server with the DAOS L2 adapter registered.

LMCache resolves ``--l2-adapter '{"type": ...}'`` against a registry that is
populated at import time, and only its own built-in adapter modules are
discovered lazily. An out-of-tree adapter therefore has to be imported
*before* the arguments are parsed -- which is all this entry point does:

    python -m lmcache_daos.mp.server --host tcp://localhost --port 5555 \\
        --chunk-size 256 --l1-size-gb 100 --eviction-policy LRU \\
        --l2-adapter '{"type":"daos","pool":"attr1","container":"kvlmc5"}'

Every other option is LMCache's own (``--help`` lists them).
"""

from __future__ import annotations

import os
import sys


def main(argv=None) -> None:
    from . import l2_adapter  # noqa: F401  (registers the 'daos' type)
    from lmcache.v1.multiprocess import server as mp_server

    if argv is not None:
        sys.argv = [sys.argv[0]] + list(argv)
    # Optional GC tuning for the cache-server process. A gen-2 collection in
    # a process that keeps thousands of MemoryObj/ObjectKey instances alive
    # stalls every in-flight request at once; DAOS_MP_GC=freeze freezes the
    # start-up heap and raises the gen-0 threshold so collections are rare.
    if os.environ.get("DAOS_MP_GC", "") == "freeze":
        import gc
        gc.collect()
        gc.freeze()
        gc.set_threshold(100000, 50, 100)
    # Experimental knobs (measurement aids; see doc/MP-MODE-PLAN.md 7.6):
    #  DAOS_MP_EVICT_TICK_S -- the L1/L2 eviction controllers poll with a
    #      hard-coded time.sleep(1); when L1 is smaller than one second of L2
    #      inflow, reserve_write hits OUT_OF_MEMORY and requests wait for the
    #      next tick. This shortens the tick for that module only.
    #  DAOS_MP_TRACE=1 -- log L1 reserve_write OUT_OF_MEMORY occurrences.
    tick = os.environ.get("DAOS_MP_EVICT_TICK_S", "")
    if tick:
        import time as _time
        from lmcache.v1.distributed.storage_controllers import eviction_controller as _ec

        class _FastTick:
            sleep = staticmethod(lambda x, _t=float(tick): _time.sleep(min(x, _t)))

            def __getattr__(self, name):
                return getattr(_time, name)

        _ec.time = _FastTick()
    if os.environ.get("DAOS_MP_TRACE", "") == "1":
        import time as _time
        from lmcache.logging import init_logger
        from lmcache.v1.distributed import l1_manager as _l1

        _log = init_logger("lmcache_daos.mp.trace")
        _orig_rw = _l1.L1Manager.reserve_write
        _state = {"oom": 0, "last": 0.0}

        def _rw(self, keys, *a, **kw):
            t0 = _time.monotonic()
            ret = _orig_rw(self, keys, *a, **kw)
            oom = sum(1 for v in ret.values() if "OUT_OF_MEMORY" in str(v[0]))
            if oom:
                _state["oom"] += oom
                now = _time.monotonic()
                if now - _state["last"] > 0.2:
                    _state["last"] = now
                    _log.info("TRACE reserve_write OOM: %d/%d keys (cumulative %d) took %.1f ms",
                              oom, len(keys), _state["oom"], (now - t0) * 1e3)
            return ret

        _l1.L1Manager.reserve_write = _rw
    args = mp_server.parse_args()
    mp_config = mp_server.parse_args_to_mp_server_config(args)
    storage_manager_config = mp_server.parse_args_to_config(args)
    obs_config = mp_server.parse_args_to_observability_config(args)
    mp_server.run_cache_server(
        mp_config=mp_config,
        storage_manager_config=storage_manager_config,
        obs_config=obs_config,
    )


if __name__ == "__main__":
    main()
