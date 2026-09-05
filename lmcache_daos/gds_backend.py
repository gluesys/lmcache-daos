"""``DaosGdsBackend`` -- LMCache storage plugin that reads KV chunks from DAOS
straight into GPU memory (``dfs_read_gpu``) and writes them from GPU memory
(``dfs_write_gpu``), bypassing host staging.

How it plugs in (LMCache 0.5.2, no upstream change)::

    storage_plugins: ["daosgds"]
    local_cpu: false                 # keep the CPU backend allocator-only
    extra_config:
      storage_plugin.daosgds.module_path: lmcache_daos.gds_backend
      storage_plugin.daosgds.class_name: DaosGdsBackend
      daosgds.pool: attr1
      daosgds.container: kvgds_s16   # replicated oclass, chunk 4194304
      daosgds.gpu_buffer_gb: 6       # GPU staging pool for get/put objects
      daosgds.io_workers: 16
    enable_async_loading: False

What the storage manager does with it (``storage_manager.py``):
- **store**: the engine allocates host objects in LocalCPUBackend (the fixed
  allocator backend); because ``get_allocator_backend()`` here returns *this*
  backend, the manager copies each object into our GPU allocator
  (``allocate_and_copy_objects``, D2D-free H2D on its copy stream) and calls
  ``batched_submit_put_task`` with the GPU copies. We write them with
  ``dfs_write_gpu``. With ``local_cpu: false`` nothing else keeps the host copy.
- **retrieve**: ``batched_get_blocking`` allocates GPU objects from our pool,
  ``dfs_read_gpu`` lands the payload in them, and the GPU connector's
  ``to_gpu`` kernel scatters from device memory into the paged KV cache. The
  host DRAM is not on the data path.

On-disk format is v2 (``serde_v2``): [4 KiB header page][payload at 4096],
header carries LMCache's ``RemoteMetadata`` so the object is self-describing
and the reader can size the GPU allocation before touching the payload.

Runtime requirements (gpudirect/README.md, "GDS over ofi+verbs"): GPU-direct
DAOS client bundle (``LMCACHE_DAOS_LIBDIR``), CUDA-enabled libfabric with the
verbs dmabuf patch, ``D_MEM_DEVICE=1``, nvidia open kernel module.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import Future
from typing import Any, Callable, List, Optional, Sequence, Union

import torch

from lmcache.logging import init_logger
from lmcache.v1.config import LMCacheEngineConfig
from lmcache.v1.memory_allocators.gpu_memory_allocator import GPUMemoryAllocator
from lmcache.v1.memory_management import MemoryAllocatorInterface, MemoryFormat, MemoryObj
from lmcache.v1.metadata import LMCacheMetadata
from lmcache.v1.storage_backend.abstract_backend import AllocatorBackendInterface
from lmcache.utils import CacheEngineKey

from . import serde_v2 as v2
from .dfs_binding import DaosError, DfsSys, warm_up_all_targets

logger = init_logger(__name__)


def _cfg(config: LMCacheEngineConfig, key: str, default=None):
    ec = config.extra_config or {}
    return ec.get(f"daosgds.{key}", default)


def _meta_pack(length: int, shapes, dtypes, fmt: MemoryFormat) -> bytes:
    """Self-contained object metadata (JSON, ~150 B). LMCache's RemoteMetadata
    codec needs process-global initialisation by the RemoteBackend, so a
    storage plugin cannot rely on it."""
    return json.dumps({
        "length": int(length),
        "shapes": [list(int(x) for x in s) for s in shapes],
        "dtypes": [str(d).replace("torch.", "") for d in dtypes],
        "fmt": int(fmt.value),
    }, separators=(",", ":")).encode()


def _meta_unpack(meta: bytes):
    d = json.loads(meta.decode())
    shapes = [torch.Size(s) for s in d["shapes"]]
    dtypes = [getattr(torch, n) for n in d["dtypes"]]
    return int(d["length"]), shapes, dtypes, MemoryFormat(int(d["fmt"]))


class DaosGdsBackend(AllocatorBackendInterface):
    def __init__(
        self,
        config: LMCacheEngineConfig,
        dst_device: str = "cuda",
        metadata: Optional[LMCacheMetadata] = None,
        local_cpu_backend=None,
        loop=None,
    ):
        assert dst_device.startswith("cuda"), "DaosGdsBackend needs a CUDA dst_device"
        super().__init__(dst_device=dst_device)
        self.config = config
        self.metadata = metadata
        self.loop = loop
        self.local_cpu_backend = local_cpu_backend
        dev = torch.device(dst_device)
        self.device_id = dev.index if dev.index is not None else torch.cuda.current_device()
        self.dst_device = f"cuda:{self.device_id}"

        pool = _cfg(config, "pool")
        cont = _cfg(config, "container")
        if not pool or not cont:
            raise ValueError("daosgds.pool and daosgds.container are required")
        self.root = str(_cfg(config, "root", v2.V2_PREFIX)).rstrip("/") or v2.V2_PREFIX
        self.io_workers = int(_cfg(config, "io_workers", 16))
        self.gpu_buffer_bytes = int(float(_cfg(config, "gpu_buffer_gb", 6)) * (1 << 30))
        self.store_enabled = bool(_cfg(config, "store", True))

        self._dfs = DfsSys(pool=pool, cont=cont, sys=_cfg(config, "sys"))
        self._dfs.mkdir_p(self.root)
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.io_workers, thread_name_prefix="daosgds-io")
        # Connect every target now (see MP-MODE-PLAN 7.7e); env DAOS_PROBE_CHUNKS.
        try:
            n = int(os.environ.get("DAOS_PROBE_CHUNKS", "64"))
            if n > 0:
                r = warm_up_all_targets(self._dfs, f"{self.root}/.daos-probe.{os.getpid()}", n, self._pool)
                logger.info("DaosGdsBackend warm-up: %d targets in %.1f ms (total %.1f ms)",
                            r["n"], r["connect_ms"], r["total_ms"])
        except Exception as e:  # pragma: no cover
            logger.warning("DaosGdsBackend warm-up failed: %s", e)

        self.memory_allocator = self.initialize_allocator(config, metadata)
        self._known: set = set()            # keys seen on DAOS (stat or our own put)
        self._known_lock = threading.Lock()
        self._put_lock = threading.Lock()
        self._put_tasks: set = set()
        self.stats = {"put": 0, "put_bytes": 0, "get": 0, "get_bytes": 0, "miss": 0,
                      "alloc_fail": 0, "get_ms": 0.0, "put_ms": 0.0}
        logger.info("DaosGdsBackend: pool=%s cont=%s root=%s device=%s gpu_buffer=%.1f GiB workers=%d",
                    pool, cont, self.root, self.dst_device, self.gpu_buffer_bytes / (1 << 30),
                    self.io_workers)

    # -- allocator backend ----------------------------------------------------
    def initialize_allocator(self, config, metadata=None) -> MemoryAllocatorInterface:
        return GPUMemoryAllocator(self.gpu_buffer_bytes, device=self.dst_device, align_bytes=4096)

    def get_memory_allocator(self) -> MemoryAllocatorInterface:
        return self.memory_allocator

    def get_allocator_backend(self):
        return self

    def allocate(self, shapes, dtypes, fmt: MemoryFormat = MemoryFormat.KV_2LTD,
                 eviction: bool = True, busy_loop: bool = True) -> Optional[MemoryObj]:
        obj = self.memory_allocator.allocate(shapes, dtypes, fmt)
        if obj is None:
            self.stats["alloc_fail"] += 1
        return obj

    def batched_allocate(self, shapes, dtypes, batch_size: int,
                         fmt: MemoryFormat = MemoryFormat.KV_2LTD,
                         eviction: bool = True, busy_loop: bool = True) -> Optional[list]:
        objs = self.memory_allocator.batched_allocate(shapes, dtypes, batch_size, fmt)
        if objs is None:
            self.stats["alloc_fail"] += 1
        return objs

    def calculate_chunk_budget(self) -> int:
        md = self.metadata
        try:
            n = 1
            for d in md.kv_shape:
                n *= int(d)
            chunk_bytes = n * torch.tensor([], dtype=md.kv_dtype).element_size()
            return max(1, self.gpu_buffer_bytes // chunk_bytes)
        except Exception:
            return 16

    # -- paths ------------------------------------------------------------------
    def _path(self, key: CacheEngineKey) -> str:
        p = v2.key_to_path(key)               # "/v2/<sha>"
        if self.root != v2.V2_PREFIX:
            p = self.root + p[len(v2.V2_PREFIX):]
        return p

    # -- lookups ----------------------------------------------------------------
    def contains(self, key: CacheEngineKey, pin: bool = False) -> bool:
        with self._known_lock:
            if key in self._known:
                return True
        try:
            present = self._dfs.stat_size(self._path(key)) is not None
        except DaosError:
            present = False
        if present:
            with self._known_lock:
                self._known.add(key)
        return present

    def batched_contains(self, keys: List[CacheEngineKey], pin: bool = False) -> int:
        """Number of leading keys present (LMCache's prefix-hit contract: the
        storage manager slices ``keys[:n]`` with the return value)."""
        hits = list(self._pool.map(lambda k: self.contains(k, pin), keys))
        n = 0
        for h in hits:
            if not h:
                break
            n += 1
        return n

    def exists_in_put_tasks(self, key: CacheEngineKey) -> bool:
        with self._put_lock:
            return key in self._put_tasks

    # -- store ------------------------------------------------------------------
    def batched_submit_put_task(self, keys: Sequence[CacheEngineKey], memory_objs: List[MemoryObj],
                                transfer_spec: Any = None,
                                on_complete_callback: Optional[Callable[[CacheEngineKey], None]] = None
                                ) -> Union[List[Future], None]:
        if not self.store_enabled:
            return None
        futs = []
        for key, obj in zip(keys, memory_objs):
            with self._put_lock:
                if key in self._put_tasks:
                    continue
                self._put_tasks.add(key)
            obj.ref_count_up()                # the manager drops its ref right after submit
            futs.append(self._pool.submit(self._put_one, key, obj, on_complete_callback))
        return futs

    def _put_one(self, key: CacheEngineKey, obj: MemoryObj, cb) -> None:
        t0 = time.perf_counter()
        try:
            if self.contains(key):
                return
            tensor = obj.tensor
            assert tensor is not None and tensor.is_cuda, "put object must be a GPU MemoryObj"
            n = obj.get_size()
            meta = _meta_pack(n, obj.get_shapes(), obj.get_dtypes(), obj.metadata.fmt)
            final = self._path(key)
            tmp = v2.temp_path(final, uuid.uuid4().hex[:8])
            h = self._dfs.open_rdwr_create(tmp)
            try:
                page = v2.pack_header(meta, n)
                import ctypes
                buf = ctypes.create_string_buffer(page, len(page))
                self._dfs.write_obj_from(h, 0, len(page), buf)
                torch.cuda.synchronize(self.device_id)   # the copy into our GPU object must have landed
                wrote = self._dfs.write_gpu_from(h, v2.payload_offset(), n, tensor.data_ptr(), self.device_id)
            finally:
                self._dfs.close_obj(h)
            if wrote != n:
                raise IOError(f"short GPU write {wrote}/{n}")
            parent = self._dfs.lookup(self.root)
            try:
                self._dfs.move(parent, tmp.rsplit("/", 1)[1], parent, final.rsplit("/", 1)[1])
            finally:
                self._dfs.release(parent)
            with self._known_lock:
                self._known.add(key)
            self.stats["put"] += 1
            self.stats["put_bytes"] += n
            self.stats["put_ms"] += (time.perf_counter() - t0) * 1e3
            if cb is not None:
                try:
                    cb(key)
                except Exception as e:  # pragma: no cover
                    logger.warning("put callback failed: %s", e)
        except Exception as e:
            logger.exception("DaosGdsBackend put %s failed: %r", key.to_string(), e)
        finally:
            obj.ref_count_down()
            with self._put_lock:
                self._put_tasks.discard(key)

    # -- retrieve ---------------------------------------------------------------
    def get_blocking(self, key: CacheEngineKey) -> Optional[MemoryObj]:
        t0 = time.perf_counter()
        path = self._path(key)
        try:
            h = self._dfs.open_rdonly(path)
        except DaosError as e:
            if getattr(e, "rc", None) == 2:
                self.stats["miss"] += 1
                return None
            raise
        obj = None
        try:
            try:
                hdr = v2.parse_header(self._dfs.read_obj(h, 0, v2.HEADER_SIZE))
            except v2.BadHeader as e:
                logger.warning("DaosGdsBackend: %s -> miss (%s)", path, e)
                self.stats["miss"] += 1
                return None
            _length, shapes, dtypes, fmt = _meta_unpack(hdr.meta)
            obj = self.memory_allocator.allocate(shapes, dtypes, fmt)
            if obj is None or obj.tensor is None:
                self.stats["alloc_fail"] += 1
                logger.warning("DaosGdsBackend: GPU buffer full, %s served as miss", path)
                return None
            n = hdr.payload_len
            if n != obj.get_size():
                logger.warning("DaosGdsBackend: %s payload %d != object %d -> miss", path, n, obj.get_size())
                obj.ref_count_down()
                return None
            got = self._dfs.read_gpu_into(h, v2.payload_offset(), n, obj.tensor.data_ptr(), self.device_id)
            if got != n:
                logger.warning("DaosGdsBackend: short GPU read %d/%d on %s -> miss", got, n, path)
                obj.ref_count_down()
                return None
            self.stats["get"] += 1
            self.stats["get_bytes"] += n
            self.stats["get_ms"] += (time.perf_counter() - t0) * 1e3
            with self._known_lock:
                self._known.add(key)
            return obj
        except Exception as e:
            logger.exception("DaosGdsBackend get %s failed: %r", path, e)
            if obj is not None:
                obj.ref_count_down()
            return None
        finally:
            self._dfs.close_obj(h)

    def batched_get_blocking(self, keys: List[CacheEngineKey]) -> List[Optional[MemoryObj]]:
        t0 = time.perf_counter()
        res = list(self._pool.map(self.get_blocking, keys))
        nb = sum(o.get_size() for o in res if o is not None)
        dt = time.perf_counter() - t0
        if nb:
            logger.info("DaosGdsBackend batched_get: %d/%d objects, %.1f MiB in %.1f ms (%.2f GB/s, GPU-direct)",
                        sum(o is not None for o in res), len(keys), nb / 2**20, dt * 1e3, nb / dt / 1e9)
        return res

    def get_non_blocking(self, key: CacheEngineKey, location: Optional[str] = None) -> Optional[Future]:
        return None

    # -- misc -------------------------------------------------------------------
    def pin(self, key: CacheEngineKey) -> bool:
        return False

    def unpin(self, key: CacheEngineKey) -> bool:
        return False

    def remove(self, key: CacheEngineKey, force: bool = True) -> bool:
        with self._known_lock:
            self._known.discard(key)
        try:
            return self._dfs.remove(self._path(key))
        except DaosError:
            return False

    def batched_remove(self, keys: List[CacheEngineKey], force: bool = True) -> int:
        return sum(1 for k in keys if self.remove(k, force))

    def touch_cache(self) -> None:
        pass

    def cancel_request(self, req_id: str) -> None:
        pass

    def close(self) -> None:
        logger.info("DaosGdsBackend stats: %s", self.stats)
        self._pool.shutdown(wait=True)
        try:
            self._dfs.close()
        except Exception:  # pragma: no cover
            pass

    def __str__(self) -> str:
        return "DaosGdsBackend"
