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
import ctypes
import hashlib
import os
import threading
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

from . import serde
from .dfs_binding import DfsSys, DaosError
from .streaming import stream_completions

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


# How many bytes past the 8-byte prefix to grab in the header read. LMCache's
# RemoteMetadata for a KV chunk serializes to ~28 B; 512 leaves ample room so
# prefix+meta come back in a single round-trip.
_HDR_CAP = 512


def _key_to_path(key) -> str:
    """Map a CacheEngineKey to a flat DFS path.

    ``key.to_string()`` can contain '/', which DFS treats as a path separator,
    so we hash to a fixed, filesystem-safe name. Directory fanout (to avoid one
    huge directory) is a Phase 4 optimization needing dfs_sys_mkdir.
    """
    s = key.to_string() if hasattr(key, "to_string") else str(key)
    return "/" + hashlib.sha256(s.encode()).hexdigest()


class DaosConnector(RemoteConnector):
    # LMCache 0.5.2 DynamicConnectorAdapter.create_connector() instantiates a
    # RemoteConnector subclass as
    #     cls(loop=..., local_cpu_backend=..., config=...)
    # (no ``url`` argument). The remote URL comes from ``config.remote_url`` and
    # is routed to this class by the ``plugin://<name>`` scheme, so we accept
    #     plugin://daos/<pool>/<container>[?sys=<sysname>]
    # and still tolerate a bare daos://<pool>/<container>.
    def __init__(self, loop=None, local_cpu_backend=None, config=None):
        if not _HAS_LMCACHE:
            raise RuntimeError("DaosConnector requires LMCache to be installed")
        if config is None:
            raise ValueError("DaosConnector requires config (config.remote_url)")
        metadata = getattr(local_cpu_backend, "metadata", None)
        super().__init__(config, metadata)

        url = config.remote_url
        parsed = urlparse(url)
        if parsed.scheme == "plugin":
            # plugin://daos/<pool>/<container> : netloc is the plugin name,
            # the pool/container live in the path.
            parts = parsed.path.strip("/").split("/", 1)
            pool = parts[0] if parts and parts[0] else ""
            cont = parts[1] if len(parts) > 1 else ""
        else:
            # daos://<pool>/<container>
            pool = parsed.netloc
            cont = parsed.path.lstrip("/")
        if not pool or not cont:
            raise ValueError(
                "daos url must be plugin://daos/<pool>/<container> "
                f"(or daos://<pool>/<container>), got {url!r}")
        sysname = parse_qs(parsed.query).get("sys", [None])[0]

        self.loop = loop
        self.local_cpu_backend = local_cpu_backend
        # dfs_sys is opened with DFS_SYS_NO_LOCK, so a single handle is NOT safe
        # for concurrent use. Over TCP the ops were slow enough to rarely
        # overlap, but over verbs/RoCE the concurrent chunk reads race and
        # corrupt each other's buffers. Give every worker thread its own
        # dfs_sys connection (each used by exactly one thread => NO_LOCK-safe)
        # so we keep the parallelism without a shared-handle data race.
        self._dfs_params = (pool, cont, sysname)
        self._tls = threading.local()
        self._handles = []
        self._handles_lock = threading.Lock()
        self._dfs = self._handle()  # primary handle for the calling thread
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="daos-io")

    def _handle(self) -> DfsSys:
        """Return this thread's own dfs_sys handle, opening one on first use."""
        h = getattr(self._tls, "dfs", None)
        if h is None:
            pool, cont, sysname = self._dfs_params
            h = DfsSys(pool=pool, cont=cont, sys=sysname)
            self._tls.dfs = h
            with self._handles_lock:
                self._handles.append(h)
        return h

    def _run(self, fn, *args):
        return self.loop.run_in_executor(self._pool, fn, *args)

    # -- RemoteConnector interface -----------------------------------------
    async def exists(self, key) -> bool:
        return await self._run(self._exists_sync, _key_to_path(key))

    def _exists_sync(self, path) -> bool:
        return self._handle().exists(path)

    def exists_sync(self, key) -> bool:
        return self._dfs.exists(_key_to_path(key))

    async def get(self, key) -> Optional["MemoryObj"]:
        return await self._run(self._get_sync, _key_to_path(key))

    # LMCache dispatches per-chunk get() concurrently, but also probes for a
    # batched hook. Advertising it lets us gather every chunk of a request onto
    # the thread pool in one shot; because _get_sync reads the payload straight
    # into the target buffer via a GIL-releasing ctypes call, the chunk reads
    # actually overlap instead of serializing on the interpreter lock.
    def support_batched_get(self) -> bool:
        return True

    async def batched_get(self, keys) -> List[Optional["MemoryObj"]]:
        import time as _t
        t0 = _t.perf_counter()
        paths = [_key_to_path(k) for k in keys]
        # return_exceptions: one unreadable object must not fail the whole
        # batch. A cache reports a miss and lets the engine recompute that
        # range. (Adopted from the main branch's batched_get.)
        gathered = await asyncio.gather(
            *(self._run(self._get_sync, p) for p in paths),
            return_exceptions=True)
        res = [None if isinstance(r, BaseException) else r for r in gathered]
        # Instrumentation: connector-side batched_get wall time. LMCache's own
        # "Retrieved ... cost" covers connector-read + H2D staging; subtracting
        # this isolates the H2D stage (env DAOS_BG_PROF=1 to enable).
        if os.environ.get("DAOS_BG_PROF") == "1" and len(keys) > 2:
            nb = 0
            for o in res:
                if o is not None:
                    try:
                        nb += len(memoryview(o.byte_array).cast("B"))
                    except Exception:
                        pass
            dt = _t.perf_counter() - t0
            import sys as _s
            _s.stderr.write(
                f"[CONN-BG] chunks={len(keys)} bytes={nb} wall={dt*1000:.1f}ms "
                f"= {nb/dt/1e9:.2f} GB/s\n")
            _s.stderr.flush()
        return res

    # LMCache's async-loading path (storage_manager) prefers this over the
    # blocking variant: it is awaited as a coroutine so the engine can overlap
    # scheduling/H2D with the DAOS reads instead of running read-all → H2D-all
    # strictly serially (that serialization is what caps retrieve at
    # read⊕H2D ≈ 19 GB/s even though read alone does 34 and H2D 46).
    # Contract (per base_connector): return only the CONSECUTIVE prefix of
    # successfully retrieved objects; release anything after the first miss.
    # MEASURED (2026-08-25): enabling this path did NOT unlock read↔H2D
    # pipelining — the API returns a list, so all chunks must still be read
    # before LMCache starts H2D. Aggregate was flat (conc4 +6%) and single
    # request regressed 1.7× (172→288ms), so we opt out and keep the blocking
    # batched_get. The implementation is retained for the day LMCache offers
    # incremental/streaming chunk delivery.
    def support_batched_get_non_blocking(self) -> bool:
        return False

    async def batched_get_non_blocking(self, lookup_id, keys):
        paths = [_key_to_path(k) for k in keys]
        res = await asyncio.gather(
            *(self._run(self._get_sync, p) for p in paths))
        prefix = []
        for obj in res:
            if obj is None:
                break
            prefix.append(obj)
        for obj in res[len(prefix):]:
            if obj is None:
                continue
            # avoid leaking allocations past the first miss
            for meth in ("ref_count_down", "release", "free"):
                fn = getattr(obj, meth, None)
                if fn is not None:
                    try:
                        fn()
                    except Exception:
                        pass
                    break
        return prefix

    # -- P4: completion-ordered streaming --------------------------------
    # Not part of LMCache 0.5.2's connector interface -- this is the reference
    # implementation for the upstream streaming-get RFC. LMCache will not call
    # it until such an API lands; until then it costs nothing and is exercised
    # by tests/bench_stream_h2d.py, which measures the overlap it enables.
    #
    # Deliberately built on the existing blocking thread pool rather than DAOS
    # event queues: measured, the event path caps near 7-12 GB/s however the
    # queues are arranged (per-EQ eqx_lock serialises submit+completion, and
    # each extra EQ costs a network context), while the blocking pool reaches
    # 34.3 GB/s on a 100 GB NVMe-resident working set. Completion ordering does
    # not require DAOS events -- a resolved future is a completion.
    def support_stream_get(self) -> bool:
        return True

    async def stream_get(self, keys, max_inflight: int = 16):
        """Yield ``(index, MemoryObj | None)`` as each chunk finishes reading.

        ``index`` is the position in ``keys``, so the consumer can copy each
        chunk into its slot the moment it lands instead of waiting for the whole
        batch. ``None`` means miss-or-error for that chunk; the stream continues
        (per-chunk error semantics, as the RFC proposes).
        """
        paths = [_key_to_path(k) for k in keys]
        async for idx, res in stream_completions(
                self.loop, self._pool, self._get_sync, paths, max_inflight):
            if isinstance(res, BaseException):
                # A single unreadable chunk must not poison the batch; the
                # engine treats it as a miss and recomputes that range.
                yield idx, None
            else:
                yield idx, res

    async def put(self, key, memory_obj: "MemoryObj"):
        # Zero-copy: alias the MemoryObj buffer instead of materialising it.
        # The old path did bytes(byte_array) → serde.pack concat → ctypes
        # create_string_buffer = THREE full copies of every chunk (120 MB of
        # GIL-held memcpy per 40 MB chunk), which measured as ~1 GB/s effective
        # store (+5.2s on a 5.24 GB store). Header and payload are written as
        # two offset writes so the payload never needs to be concatenated.
        view = memory_obj.byte_array
        if not isinstance(view, memoryview):
            view = memoryview(view)
        view = view.cast("B")
        n = len(view)
        meta_bytes = RemoteMetadata(
            n,
            memory_obj.get_shapes(),
            memory_obj.get_dtypes(),
            memory_obj.get_memory_format(),
        ).serialize()
        header = serde.prefix_pack(len(meta_bytes), n) + meta_bytes
        src = (ctypes.c_char * n).from_buffer(view)
        await self._run(self._put_sync, _key_to_path(key), header, src, n)

    def _put_sync(self, path, header, src, n):
        dfs = self._handle()
        obj = dfs.open_rdwr_create(path)
        try:
            hdr = (ctypes.c_char * len(header)).from_buffer_copy(header)
            dfs.write_obj_from(obj, 0, len(header), hdr)       # tiny (~36 B)
            dfs.write_obj_from(obj, len(header), n, src)       # bulk, no copy
        finally:
            dfs.close_obj(obj)

    async def list(self) -> List[str]:
        # Optional for a cache backend; requires dfs_sys_opendir/readdir
        # marshalling (Phase 4). Enumeration is never needed on the hot path.
        return []

    async def close(self):
        self._pool.shutdown(wait=True)
        with self._handles_lock:
            handles = list(self._handles)
        for h in handles:
            try:
                h.close()
            except Exception:
                pass

    # -- runs inside the thread pool ---------------------------------------
    @staticmethod
    def _release(memory_obj) -> None:
        """Hand a MemoryObj back to the allocator (best effort).

        Needed on the torn-object path: reading straight into the destination
        means the buffer is allocated *before* the payload length is known to be
        good, so a short read has to give it back or it leaks.
        """
        for meth in ("ref_count_down", "release", "free"):
            fn = getattr(memory_obj, meth, None)
            if fn is not None:
                try:
                    fn()
                except Exception:
                    pass
                return

    def _get_sync(self, path) -> Optional["MemoryObj"]:
        """Load one object, or return None if it is absent OR incomplete.

        Every length is checked and any shortfall is reported as a plain miss.
        A writer killed mid-store leaves a short file behind and `exists()` is
        only an open(), so a torn object still looks present; raising here would
        surface as a failed request, because vLLM's default
        ``kv_load_failure_policy`` is ``fail`` rather than recompute.

        The checks are the same ones the main branch performs, but they cost
        nothing here because the payload is never materialised: the destination
        is the MemoryObj buffer and ``read_obj_into`` returns the byte count, so
        a truncated payload is detected from the return value. Measured
        (tests/bench_readpath_merge.py, 32 x 28 MiB):

            arm                          1 thread   16 threads
            main-style (4 opens, 2 copies)   2.78        2.61
            zero-copy, no checks           14.37       33.55
            zero-copy + these checks       12.31       32.75

        main's path does not scale at all -- the two full Python-level copies
        hold the GIL, so 16 threads serialise. Keeping its safety costs 2%.
        """
        dfs = self._handle()  # this worker thread's own dfs_sys handle
        # One open for prefix + metadata + payload (was 3 opens: exists + meta +
        # payload). Missing key => open raises ENOENT, which we map to None.
        try:
            obj = dfs.open_rdonly(path)
        except DaosError as e:
            if getattr(e, "rc", None) == 2:  # ENOENT
                return None
            raise
        try:
            # One small read grabs the prefix AND the (tiny) metadata together
            # -- KV RemoteMetadata is ~28 B, so prefix+meta almost always fit in
            # _HDR_CAP, cutting the per-chunk round-trips from 3 reads to 2
            # (header + payload). Only a pathologically large meta needs a
            # second read.
            ps = serde.prefix_size()
            hdr = dfs.read_obj(obj, 0, ps + _HDR_CAP)
            if len(hdr) < ps:
                return None                      # empty or mid-prefix
            meta_len, payload_len = serde.parse_prefix(hdr[:ps])
            if meta_len <= len(hdr) - ps:
                meta_bytes = hdr[ps:ps + meta_len]
            else:
                meta_bytes = dfs.read_obj(obj, ps, meta_len)
            if len(meta_bytes) != meta_len:
                return None                      # mid-metadata
            try:
                metadata = RemoteMetadata.deserialize(meta_bytes)
            except Exception:
                return None                      # unparseable header
            if payload_len < metadata.length:
                return None                      # header itself is inconsistent

            memory_obj = self.local_cpu_backend.allocate(
                metadata.shapes, metadata.dtypes, metadata.fmt)
            if memory_obj is None:
                return None

            # Read the (large) payload straight into the target MemoryObj
            # buffer: no intermediate bytes object, no second GIL-held memcpy.
            # dfs_sys_read is a ctypes C call that releases the GIL, so with the
            # thread-pool + per-thread handles the concurrent chunk reads truly
            # overlap -- this is the retrieve-pipeline parallelization. (Over
            # UCX the aliased buffer reads back correctly; the corruption seen
            # earlier was the libfabric verbs;ofi_rxm bug, not this alias.)
            view = memory_obj.byte_array
            if not isinstance(view, memoryview):
                view = memoryview(view)
            view = view.cast("B")
            n = metadata.length
            dest = (ctypes.c_char * n).from_buffer(view[:n])
            got = dfs.read_obj_into(
                obj, serde.prefix_size() + meta_len, payload_len, dest)
            if got != payload_len:
                # Truncated payload: give the buffer back and report a miss.
                self._release(memory_obj)
                return None
            return memory_obj
        finally:
            dfs.close_obj(obj)
