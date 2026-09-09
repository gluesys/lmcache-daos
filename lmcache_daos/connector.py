# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
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
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

from . import serde
from .dfs_binding import DfsSys, DaosError, warm_up_all_targets
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
        self._workers = 16
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._workers, thread_name_prefix="daos-io")
        self._warm_up()

    def _warm_up(self) -> None:
        """Connect to every target now, not on the first user request.

        Same exposure as the MP adapter (doc/MP-MODE-PLAN.md 7.7e): a lazy
        first RPC to a target in the middle of a store can wait ~16 s for the
        rdma_cm handshake on a lossy fabric. ``DAOS_PROBE_CHUNKS`` (default
        64, must be >= target count; 0 disables) sets the SX probe size.
        """
        import sys as _s
        import time as _t
        try:
            nchunks = int(os.environ.get("DAOS_PROBE_CHUNKS", "64"))
        except ValueError:
            nchunks = 64
        if nchunks <= 0:
            return
        t0 = _t.monotonic()
        try:
            r = warm_up_all_targets(self._dfs, f"/.daos-probe.{os.getpid()}",
                                    nchunks, self._pool)
            _s.stderr.write(f"[DaosConnector] warm-up: SX probe, {r['n']} targets contacted "
                            f"in {r['connect_ms']:.1f} ms, {r['ok']}/{r['n']} chunks ok, "
                            f"total {r['total_ms']:.1f} ms\n")
        except Exception as e:  # best effort
            _s.stderr.write(f"[DaosConnector] warm-up failed ({e}) after "
                            f"{(_t.monotonic() - t0) * 1e3:.1f} ms\n")
        _s.stderr.flush()

    def _run(self, fn, *args):
        return self.loop.run_in_executor(self._pool, fn, *args)

    # -- RemoteConnector interface -----------------------------------------
    async def exists(self, key) -> bool:
        return await self._run(self._exists_sync, _key_to_path(key))

    def _exists_sync(self, path) -> bool:
        return self._dfs.exists(path)

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
        """Fetch every key. Unlike the *_non_blocking variant this has no prefix
        semantics -- the result is positional, with None for a miss."""
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

    @staticmethod
    def _drop_put_ref(memory_obj) -> None:
        """Release the reference the serializer took on our behalf.

        Required by LMCache's contract, which is only visible if you read the
        serializer next to the backend. NaiveSerializer.serialize() is::

            def serialize(self, memory_obj):
                memory_obj.ref_count_up()
                return memory_obj

        -- the same object, with one reference added FOR THE CONSUMER. And
        remote_backend.batched_submit_put_task() drops only its own::

            for mo in memory_objs: mo.ref_count_up()
            try:     compressed = [serialize(mo) for mo in memory_objs]
            finally: for mo in memory_objs: mo.ref_count_down()
            ... connection.batched_put(keys, compressed_memory_objs)

        So every memory_obj arriving at put()/batched_put() carries a reference
        that the connector owns and must release. This connector never did, on
        either path, and support_batched_put() is True so the batched one is the
        one in use -- a leaked reference per stored chunk.

        A leak does not corrupt by itself; it stops the CPU pool from ever
        reclaiming. What makes it a correctness problem is what the allocator
        then does under pressure on the get path, where _get_sync() calls
        local_cpu_backend.allocate() for every chunk. LMCache already reports
        "Ref count of MemoryObj ... is negative: -1. Double free occurred
        somewhere" on this path in the hundreds per run and never under
        LocalCPUBackend, so its accounting is demonstrably inconsistent here.

        Written as its own method rather than reusing _release(): that one tries
        ref_count_down, then release, then free, for the torn-object path where
        any of them will do. Here exactly one ref_count_down is owed, so
        falling through to a different method would be wrong.
        """
        fn = getattr(memory_obj, "ref_count_down", None)
        if fn is None:
            return
        try:
            fn()
        except Exception:
            pass

    async def put(self, key, memory_obj: "MemoryObj"):
        header, src, n = self._prep_write(memory_obj)
        try:
            await self._run(self._put_sync, _key_to_path(key), header, src, n)
        finally:
            self._drop_put_ref(memory_obj)

    # Aliasing the MemoryObj on the store path is UNSAFE and is off by default.
    # See _prep_write. Set DAOS_UNSAFE_ALIAS_STORE=1 only to reproduce the bug.
    _ALIAS_STORE = os.environ.get("DAOS_UNSAFE_ALIAS_STORE") == "1"

    # Diagnostic for the KV corruption: read into a private bytearray and copy
    # into the MemoryObj, instead of letting DAOS write straight into the
    # MemoryObj's (torch-backed) memory.
    #
    # It discriminates the one hypothesis left standing. Concurrency is the
    # trigger -- 0/200 sequential against 6/208 concurrent -- and the failures
    # start at exact 4 MiB DFS chunk boundaries and lose exactly one or two
    # chunks, with the head of the buffer holding the CORRECT key's data. The
    # same 28 MiB at the same thread count into a plain bytearray
    # (tests/test_rawio_integrity.py) is byte-exact 80/80, so the destination
    # buffer is the variable that decides whether the defect appears.
    #
    #   corruption disappears -> the aliased MemoryObj destination is the
    #       cause, and the fix is a copy here or a registered/pinned buffer
    #   corruption persists   -> the destination is innocent and the fault is
    #       in concurrent multi-chunk DFS reads, one layer down
    #
    # Off by default: it reintroduces the full copy per chunk that the aliased
    # read exists to avoid.
    _READ_VIA_BYTEARRAY = os.environ.get("DAOS_READ_VIA_BYTEARRAY") == "1"

    def _prep_write(self, memory_obj):
        """Build ``(header, src, n)`` for the write.

        History, because the obvious "optimisation" here is a correctness bug.

        The original path did ``bytes(byte_array)`` -> ``serde.pack`` concat ->
        ``create_string_buffer``: THREE full copies of every chunk, 120 MB of
        GIL-held memcpy per 40 MB chunk, ~1 GB/s effective store (+5.2 s on a
        5.24 GB store). That was replaced by having ``src`` merely *alias* the
        MemoryObj buffer, with header and payload written as two offset writes
        so the payload is never concatenated: +5178 -> +65 ms at 8K.

        The alias is unsafe in principle: ``put()`` is driven from LMCache's
        ``batched_put()``, which is an ASYNC SUBMIT, and LMCache drops its
        reference (``ref_count_down``) and recycles the MemoryObj without
        waiting for us -- so an aliased write can land after the buffer has
        become someone else's KV. Nothing fails: the write succeeds, the sizes
        agree, and the object holds the wrong tensor.

        Copying is the default as a precaution, and that is ALL the evidence
        supports. Do not read it as a fix.

        The honest history: this was claimed as the cause, retracted, re-claimed
        with a "100% vs 10%" rate comparison, and then that comparison turned
        out to be invalid too. The 10% came from a measurement whose reference
        pass was itself a cache hit -- prompts repeated across runs, so pass A
        read the same stale object as pass B and corrupt-matching-corrupt scored
        as success. With the instrument fixed (per-run nonce, pass A asserted to
        miss, container verified empty, non-perturbing checks) the rates are:

            aliased store : 100%  95% CI [83.9%, 100%]
            copied store  :  85%  95% CI [64.0%, 94.8%]

        Those intervals overlap, so copying is NOT shown to help. It stays
        because the alias is unsafe on its own terms -- batched_put() is an
        async submit and LMCache ref_count_downs without waiting -- and because
        it costs nothing measurable: one from_buffer_copy is not the old
        three-copy path, it runs in this connector's thread pool rather than on
        the latency path, and LMCache's reported store cost is unchanged either
        way (offload ~62 ms, put_time ~0.12 ms).

        The path is broken roughly 85% of the time either way. What IS
        established is the layer: raw DFS reads and writes are byte-exact at 16
        threads (tests/test_rawio_integrity.py, 80/80) while end-to-end KV
        fails 85%, so the fault is in how this connector uses DFS or in the
        LMCache integration -- not in transport or storage.

        The zero-copy write can be recovered once the residual failures are
        understood, but by PINNING the MemoryObj for the duration
        (``pin()``/``unpin()``, as ``cache_engine`` does), not by reinstating an
        unheld alias.
        """
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
        buf = (ctypes.c_char * n)
        if self._ALIAS_STORE:
            return header, buf.from_buffer(view), n
        return header, buf.from_buffer_copy(view), n

    def _put_sync(self, path, header, src, n):
        obj = self._dfs.open_rdwr_create(path)
        try:
            hdr = (ctypes.c_char * len(header)).from_buffer_copy(header)
            self._dfs.write_obj_from(obj, 0, len(header), hdr)   # tiny (~36 B)
            self._dfs.write_obj_from(obj, len(header), n, src)   # bulk, no copy
        finally:
            self._dfs.close_obj(obj)

    # -- batched interface --------------------------------------------------
    # Only get/put are overridden, and only because measurement said so.
    #
    # Every chunk is its own DFS object, so these are concurrent fan-outs over
    # the thread pool, not true batch RPCs the way Redis's batch_exists_sync is.
    # Collapsing N operations into one request needs the dkey/akey layout where
    # the chunks of a prefix share a dkey.
    #
    # Measured on nvme_pool, 19 objects per batch (a 4864-token prefix):
    #
    #   chunk    PUT seq -> batched        GET seq -> batched
    #    1 MiB   106.8 -> 44.4 ms  2.41x    69.3 -> 19.8 ms  3.49x
    #    4 MiB   248.1 -> 152.8 ms 1.62x   133.9 -> 54.9 ms  2.44x
    #   16 MiB   862.4 -> 643.6 ms 1.34x   436.1 -> 148.1 ms 2.95x
    #
    # batched_contains is deliberately NOT overridden. It looks like the obvious
    # win -- the inherited fallback is a *sequential* `for key in keys:
    # contains(key)` loop, 128 round trips for a 32K prefix -- but dfs_sys_open +
    # close on an existing object costs only ~69 us, so the whole sequential
    # probe is 8.8 ms and thread-dispatch overhead cancels the parallelism: a
    # windowed fan-out measured 9.4 ms, i.e. 0.9x. Against a ~500 ms retrieve it
    # is noise either way. Left inherited (support_batched_contains() -> False)
    # rather than carrying code that buys nothing. Worth revisiting only with
    # data from a low-hit-rate fleet, where the argument would be about wasted
    # server operations rather than latency.
    #
    # batched_async_contains and batched_get_non_blocking are also left
    # inherited: both already fan out via asyncio.gather over our per-key
    # methods, and get_non_blocking carries the ref_count_down() discipline for
    # objects after the first failure -- reimplementing that risks a MemoryObj
    # leak for no measured gain.


    def support_batched_put(self) -> bool:
        return True

    async def batched_put(self, keys, memory_objs):
        # Zero-copy, same as put(). main's version built every blob first via
        # _pack() -- for a 5.24 GB store that is 10.5 GB of GIL-held memcpy on
        # the asyncio loop thread plus a 5.24 GB transient. _prep_write only
        # aliases each buffer, so nothing is materialised here.
        prepped = [self._prep_write(mo) for mo in memory_objs]
        try:
            await asyncio.gather(*(
                self._run(self._put_sync, _key_to_path(k), h, s, n)
                for k, (h, s, n) in zip(keys, prepped)
            ))
        finally:
            # One reference owed per object -- see _drop_put_ref. This is the
            # path LMCache actually uses, since support_batched_put() is True.
            for mo in memory_objs:
                self._drop_put_ref(mo)



    async def list(self) -> List[str]:
        """Enumerate the object names in the container.

        LIMITATION -- the names are NOT reversible to ``CacheEngineKey``.
        ``_key_to_path`` hashes the key with sha256, so what comes back here is
        the 64-char digest. That is enough for capacity work (count, total
        bytes, sweep-and-delete by path) but not for consumers that expect to
        rebuild keys from names: LMCache's own ``fs_connector`` encodes the key
        into the filename ('/' -> '-SEP-') and
        ``internal_api_server/vllm/load_fs_chunks_api`` reverses it with
        ``CacheEngineKey.from_string``. Making our names reversible means
        changing the on-disk naming scheme, which invalidates every cached
        object and interacts with the directory-fanout design -- so it is left
        as an explicit decision rather than a silent change.
        """
        return await self._run(self._dfs.listdir, "/")

    def remove_sync(self, key) -> bool:
        """Delete one object. ``RemoteBackend.remove()`` calls this, so this is
        what makes remote eviction work at all -- without it the container grows
        without bound."""
        return self._dfs.remove(_key_to_path(key))

    async def close(self):
        self._pool.shutdown(wait=True)
        try:
            self._dfs.close()
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
        dfs = self._dfs
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
            off = serde.prefix_size() + meta_len

            if self._READ_VIA_BYTEARRAY:
                # Experiment: read into a private bytearray, then copy. Costs
                # one full copy per chunk, which is exactly what the aliased
                # path exists to avoid -- see _READ_VIA_BYTEARRAY.
                tmp = bytearray(payload_len)
                got = dfs.read_obj_into(
                    obj, off, payload_len,
                    (ctypes.c_char * payload_len).from_buffer(tmp))
                if got != payload_len:
                    self._release(memory_obj)
                    return None
                view[:n] = memoryview(tmp)[:n]
                return memory_obj

            dest = (ctypes.c_char * n).from_buffer(view[:n])
            got = dfs.read_obj_into(obj, off, payload_len, dest)
            if got != payload_len:
                # Truncated payload: give the buffer back and report a miss.
                self._release(memory_obj)
                return None
            return memory_obj
        finally:
            dfs.close_obj(obj)
