"""LMCache RemoteConnector backed by a DAOS DFS (dfs_sys) namespace.

Pinned to LMCache **v0.5.2**. Verified against that tag:
  * connector ctor signature: ``(self, url, loop, local_cpu_backend)``;
    base ``RemoteConnector.__init__(config, metadata)`` is fed from
    ``local_cpu_backend.config`` / ``.metadata``
    (lmcache/v1/storage_backend/connector/redis_connector.py)
  * metadata codec: ``lmcache.v1.protocol.RemoteMetadata(length, shapes, dtypes,
    fmt)`` with ``.serialize()`` / ``.deserialize()`` (shapes/dtypes are LISTS)
  * MemoryObj reconstruction: ``local_cpu_backend.allocate(shapes, dtypes, fmt)``
    then copy bytes into ``memory_obj.byte_array``
  * registration: config ``remote_storage_plugins`` + ``extra_config``
    module_path/class_name; a RemoteConnector subclass is auto-wrapped in
    LMCache's ``DynamicConnectorAdapter`` (connector/__init__.py::CreateConnector)

Wire-up (config.yaml)::

    remote_url: "daos://<pool>/<container>[?sys=<sysname>]"
    remote_storage_plugins: ["daos"]
    extra_config:
      remote_storage_plugin.daos.module_path: lmcache_daos.connector
      remote_storage_plugin.daos.class_name: DaosConnector

Blocking libdfs calls run in a thread pool so LMCache's asyncio loop is never
blocked.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

from . import serde
from .dfs_binding import DfsSys

# LMCache is only present on the serving host. Guard the import so this module
# stays inspectable/unit-testable elsewhere.
try:
    from lmcache.v1.storage_backend.connector.base_connector import RemoteConnector
    from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend  # noqa: F401
    from lmcache.v1.memory_management import MemoryObj  # noqa: F401
    from lmcache.v1.protocol import RemoteMetadata

    _HAS_LMCACHE = True
except Exception:  # pragma: no cover - exercised only off the serving host
    _HAS_LMCACHE = False

    class RemoteConnector:  # minimal stand-in so the class body imports
        def __init__(self, *a, **k):
            pass


def _parse_daos_url(url: str):
    """Parse a DAOS target out of either URL spelling.

    LMCache's ``DynamicConnectorAdapter`` builds its schema as
    ``plugin://<plugin_type>`` and only matches URLs with that prefix -- a
    plain ``daos://`` url is never routed to us. Since ``can_parse`` uses
    ``startswith``, extra path components survive, so the pool/container ride
    along in the path:

        plugin://daos/<pool>/<container>[?sys=<sysname>]

    ``daos://<pool>/<container>`` is still accepted for direct construction
    (unit tests, embedding this connector without LMCache's factory).
    """
    parsed = urlparse(url)
    if parsed.scheme == "plugin":
        # netloc is the plugin name ("daos" / "daos.instance"); pool+cont are path
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            raise ValueError(
                "daos plugin url must be plugin://<name>/<pool>/<container>, "
                f"got {url!r}")
        pool, cont = parts[0], parts[1]
    else:
        pool = parsed.netloc
        parts = [p for p in parsed.path.split("/") if p]
        cont = parts[0] if parts else ""
    if not pool or not cont:
        raise ValueError(
            "daos url must be daos://<pool>/<container> or "
            f"plugin://<name>/<pool>/<container>, got {url!r}")
    return pool, cont, parse_qs(parsed.query).get("sys", [None])[0]


def _key_to_path(key) -> str:
    """Map a CacheEngineKey to a flat DFS path.

    ``key.to_string()`` can contain '/', which DFS treats as a path separator,
    so we hash to a fixed, filesystem-safe name. Directory fanout (to avoid one
    huge directory) is a Phase 4 optimization needing dfs_sys_mkdir.
    """
    s = key.to_string() if hasattr(key, "to_string") else str(key)
    return "/" + hashlib.sha256(s.encode()).hexdigest()


class DaosConnector(RemoteConnector):
    def __init__(self, url=None, loop=None, local_cpu_backend=None, config=None):
        # LMCache's DynamicConnectorAdapter instantiates us as
        #   cls(loop=..., local_cpu_backend=..., config=...)
        # -- no url argument -- so the target is taken from config.remote_url.
        # Direct construction (tests) may still pass url positionally.
        if not _HAS_LMCACHE:
            raise RuntimeError("DaosConnector requires LMCache to be installed")
        if local_cpu_backend is None:
            raise ValueError("DaosConnector requires a local_cpu_backend")
        cfg = config if config is not None else local_cpu_backend.config
        super().__init__(cfg, local_cpu_backend.metadata)

        if url is None:
            url = getattr(cfg, "remote_url", None)
            if not url:
                raise ValueError(
                    "DaosConnector: no url given and config.remote_url is unset")
        pool, cont, sysname = _parse_daos_url(url)

        self.loop = loop
        self.local_cpu_backend = local_cpu_backend
        self._dfs = DfsSys(pool=pool, cont=cont, sys=sysname)
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="daos-io")

    def _run(self, fn, *args):
        return self.loop.run_in_executor(self._pool, fn, *args)

    # -- RemoteConnector interface -----------------------------------------
    async def exists(self, key) -> bool:
        return await self._run(self._dfs.exists, _key_to_path(key))

    def exists_sync(self, key) -> bool:
        return self._dfs.exists(_key_to_path(key))

    async def get(self, key) -> Optional["MemoryObj"]:
        return await self._run(self._get_sync, _key_to_path(key))

    async def put(self, key, memory_obj: "MemoryObj"):
        # Extract on the calling thread (cheap), do the write in the pool.
        kv_bytes = bytes(memory_obj.byte_array)
        meta_bytes = RemoteMetadata(
            len(kv_bytes),
            memory_obj.get_shapes(),
            memory_obj.get_dtypes(),
            memory_obj.get_memory_format(),
        ).serialize()
        blob = serde.pack(meta_bytes, kv_bytes)
        await self._run(self._dfs.write, _key_to_path(key), blob)

    async def list(self) -> List[str]:
        # Optional for a cache backend; requires dfs_sys_opendir/readdir
        # marshalling (Phase 4). Enumeration is never needed on the hot path.
        return []

    async def close(self):
        self._pool.shutdown(wait=True)
        self._dfs.close()

    # -- runs inside the thread pool ---------------------------------------
    def _get_sync(self, path) -> Optional["MemoryObj"]:
        if not self._dfs.exists(path):
            return None
        meta_len, payload_len = serde.parse_prefix(
            self._dfs.read(path, 0, serde.prefix_size()))
        meta_bytes = self._dfs.read(path, serde.prefix_size(), meta_len)
        metadata = RemoteMetadata.deserialize(meta_bytes)

        memory_obj = self.local_cpu_backend.allocate(
            metadata.shapes, metadata.dtypes, metadata.fmt)
        if memory_obj is None:
            return None

        kv_bytes = self._dfs.read(
            path, serde.prefix_size() + meta_len, payload_len)

        view = memory_obj.byte_array
        if isinstance(view, memoryview):
            if view.format == "<B":
                view = view.cast("B")
        else:
            view = memoryview(view)
        view[: metadata.length] = kv_bytes
        return memory_obj
