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
