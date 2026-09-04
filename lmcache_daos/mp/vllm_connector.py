"""vLLM-side connector shim for MP mode.

vLLM 0.18 bundles its own copy of ``LMCacheMPConnector`` and resolves the
registered name *before* honouring ``kv_connector_module_path``. That copy
predates LMCache 0.5.2's ``LMCacheMPSchedulerAdapter(server_urls=[...])``
signature and dies with ``ZMQError: Invalid argument (addr='t')`` (a URL
string iterated character by character). Exposing LMCache's own connector
under an unregistered name side-steps the stale copy::

    --kv-transfer-config '{"kv_connector":"DaosMPConnector",
        "kv_connector_module_path":"lmcache_daos.mp.vllm_connector",
        "kv_role":"kv_both",
        "kv_connector_extra_config":{"lmcache.mp.host":"tcp://localhost",
                                     "lmcache.mp.port":5555}}'

Nothing DAOS-specific lives here; the DAOS side is the L2 adapter inside the
MP server process.
"""

# LMCache ships per-vLLM-version connectors. The generic module needs a vLLM
# newer than 0.18 (imports KVCacheSpecKind); pick the variant matching the
# installed vLLM, falling back to the generic one.
try:
    import vllm as _vllm

    _VLLM = tuple(int(x) for x in _vllm.__version__.split(".")[:2])
except Exception:  # pragma: no cover
    _VLLM = (0, 0)

# LMCache 0.5.2's own 0.18 variant is stale in one respect: it passes the
# server URL as a plain string where LMCacheMPSchedulerAdapter now takes a
# list (the class still normalises the other legacy positional arguments).
# Coerce it here so the shipped connector works unmodified.
from lmcache.integration.vllm import vllm_multi_process_adapter as _vmpa

if not getattr(_vmpa.LMCacheMPSchedulerAdapter, "_daos_urls_patched", False):
    _orig_init = _vmpa.LMCacheMPSchedulerAdapter.__init__

    def _init(self, server_urls, *args, **kwargs):
        if isinstance(server_urls, str):
            server_urls = [server_urls]
        return _orig_init(self, server_urls, *args, **kwargs)

    _vmpa.LMCacheMPSchedulerAdapter.__init__ = _init
    _vmpa.LMCacheMPSchedulerAdapter._daos_urls_patched = True

if _VLLM == (0, 18):
    from lmcache.integration.vllm.lmcache_mp_connector_0180 import (  # noqa: F401
        LMCacheMPConnector as DaosMPConnector,
    )
elif _VLLM == (0, 20) or _VLLM == (0, 19):
    from lmcache.integration.vllm.lmcache_mp_connector_0201 import (  # noqa: F401
        LMCacheMPConnector as DaosMPConnector,
    )
else:
    from lmcache.integration.vllm.lmcache_mp_connector import (  # noqa: F401
        LMCacheMPConnector as DaosMPConnector,
    )

__all__ = ["DaosMPConnector"]
