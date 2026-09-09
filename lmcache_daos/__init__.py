# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""DAOS storage backends for LMCache (developed against LMCache v0.5.2).

Three data planes share this package:

* :mod:`lmcache_daos.connector` -- in-process ``RemoteConnector`` plugin;
* :mod:`lmcache_daos.mp` -- ``L2AdapterInterface`` behind LMCache's
  multiprocess cache server, so several vLLM instances share one pinned L1;
* :mod:`lmcache_daos.gds_backend` -- **experimental** GPU-direct backend that
  reads DAOS straight into GPU memory. Not for production; see the README.

This is the single source of the version: ``pyproject.toml`` reads
``__version__`` from here.
"""

from .serde import pack, unpack, prefix_size, parse_prefix

__all__ = ["pack", "unpack", "prefix_size", "parse_prefix", "__version__"]
__version__ = "0.1.0"
