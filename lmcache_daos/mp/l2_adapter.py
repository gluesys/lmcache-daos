"""DAOS L2 adapter for LMCache multiprocess (MP) mode.

MP mode runs the cache engine in a separate ZMQ server process that owns L1
(pinned CPU memory) and a list of L2 adapters. L2 is *not* the
``RemoteConnector`` interface used by the in-process ``LMCacheConnectorV1``;
it is ``lmcache.v1.distributed.l2_adapters.base.L2AdapterInterface`` -- a
non-blocking, batch-oriented contract:

    submit_*_task(...) -> task_id          (store / lookup_and_lock / load)
    eventfd is signalled when a task completes
    pop_completed_store_tasks() / query_*_result(task_id)   (result once)

Store and load are handed the caller's ``MemoryObj`` buffers, so this adapter
reads DAOS payloads **straight into pinned L1 memory** (``obj.byte_array`` is a
writable memoryview) and writes from it -- no intermediate copy either way.

Design choices (see doc/MP-MODE-PLAN.md):

* One DFS object per (key): raw payload bytes only, no header. The layout is
  known to the caller, so the only integrity check needed is *length*: a
  lookup advertises a hit only if ``st_size`` equals the expected size, and a
  load succeeds only if ``bytes read == len(buffer)``. A partially written
  object therefore reads as a miss, never as garbage (the same guarantee the
  in-process connector gets from its 8-byte prefix, at zero header cost).
* Idempotent store: an object that already exists with the right size is
  skipped. Writers race benignly; the last complete write wins and both are
  identical by construction (same key == same content).
* Locking is client-side reference counting, as in LMCache's own
  ``native_connector_l2_adapter``: DAOS does not evict behind our back, so a
  lock only has to stop *our* ``delete()`` from removing an object that a
  prefetch is about to load.
* Blocking libdfs calls run on a private thread pool; ``dfs_sys_read`` /
  ``dfs_sys_write`` release the GIL, so per-key I/O inside a task really does
  overlap.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import os
import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from lmcache.logging import init_logger
from lmcache.native_storage_ops import Bitmap
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.internal_api import L2StoreResult
from lmcache.v1.distributed.l2_adapters.base import L2AdapterInterface, L2TaskId
from lmcache.v1.distributed.l2_adapters.config import (
    L2AdapterConfigBase,
    register_l2_adapter_type,
)
from lmcache.v1.distributed.l2_adapters.factory import register_l2_adapter_factory
from lmcache.v1.platform import create_event_notifier

from ..dfs_binding import DaosError, DfsSys

logger = init_logger(__name__)

# daos_obj_class.h: OBJ_CLASS_DEF(OR_RP_1, MAX_NUM_GROUPS) -- one shard on
# every target of the pool. Used only for the start-up probe (see _warm_up).
OC_SX = (1 << 24) | 0xFFFF

ADAPTER_TYPE = "daos"

# Same wire format as native_connector_l2_adapter / fs_l2_adapter so keys stay
# reversible and greppable. ObjectKey forbids '@' in its string fields, so the
# separator is unambiguous. '/' is legal in model names (HF ids) and would
# create directories, so it is folded to '_'.
_SEP = "@"


def _key_to_name(key: ObjectKey) -> str:
    model = key.model_name.replace("/", "_")
    base = (
        f"{model}{_SEP}{key.kv_rank:08x}{_SEP}{key.object_group_id:x}"
        f"{_SEP}{key.chunk_hash.hex()}"
    )
    if key.cache_salt:
        return f"{base}{_SEP}{key.cache_salt.replace('/', '_')}"
    return base


def _expected_bytes(layout: Optional[MemoryLayoutDesc]) -> Optional[int]:
    """Byte size implied by a MemoryLayoutDesc, or None if unknown."""
    if layout is None:
        return None
    try:
        import torch  # noqa: PLC0415  (only needed for element sizes)

        total = 0
        for shape, dtype in zip(layout.shapes, layout.dtypes):
            total += math.prod(shape) * torch.empty(0, dtype=dtype).element_size()
        return total
    except Exception:  # pragma: no cover - advisory only
        return None


class _FairDispatcher:
    """Round-robin key scheduler across concurrent load tasks.

    ``pool.map`` per task is FIFO by task: with 12 requests in flight on a
    saturated 34 GB/s link the first task finishes in ~25 ms and the last in
    ~230 ms, so TTFT p95 is ~2x p50 (measured: 235 / 465 ms). Interleaving
    the keys of all active tasks one-by-one makes the tasks progress
    together (processor sharing): p95 drops toward the wave time at the cost
    of a higher p50. Total throughput is unchanged -- the link is the limit.
    """

    def __init__(self, pool: concurrent.futures.ThreadPoolExecutor, slots: int):
        self._pool = pool
        self._slots = threading.Semaphore(slots)
        self._cv = threading.Condition()
        self._queues: Dict[int, Any] = {}   # token -> deque of (idx, item, fn, state)
        self._order: List[int] = []
        self._rr = 0
        self._next = 0
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="daos-l2-fair", daemon=True)
        self._thread.start()

    def run(self, fn, items) -> list:
        """Schedule ``fn`` over ``items`` fairly against other callers; block
        until all complete; return results in input order."""
        import collections
        n = len(items)
        state = {"results": [None] * n, "left": n, "done": threading.Event()}
        if n == 0:
            return []
        with self._cv:
            token = self._next
            self._next += 1
            self._queues[token] = collections.deque(
                (i, it, fn, state) for i, it in enumerate(items))
            self._order.append(token)
            self._cv.notify()
        state["done"].wait()
        return state["results"]

    def _loop(self) -> None:
        while not self._stop:
            with self._cv:
                while not self._order and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                # next non-empty queue in round-robin order
                self._rr %= len(self._order)
                token = self._order[self._rr]
                q = self._queues[token]
                idx, item, fn, state = q.popleft()
                if q:
                    self._rr += 1
                else:
                    del self._queues[token]
                    self._order.pop(self._rr)
            self._slots.acquire()          # wait for an I/O slot
            self._pool.submit(self._one, fn, item, idx, state)

    def _one(self, fn, item, idx, state) -> None:
        try:
            state["results"][idx] = fn(item)
        except Exception as e:  # pragma: no cover
            logger.warning("fair dispatcher item failed: %s", e)
            state["results"][idx] = False
        finally:
            self._slots.release()
            with self._cv:
                state["left"] -= 1
                if state["left"] == 0:
                    state["done"].set()

    def close(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()


class DaosL2AdapterConfig(L2AdapterConfigBase):
    """``--l2-adapter`` JSON for the DAOS adapter::

        {"type": "daos", "pool": "attr1", "container": "kvlmc5",
         "root": "/mp", "workers": 8, "max_capacity_gb": 0,
         "verify_size": true}
    """

    def __init__(
        self,
        pool: str,
        container: str,
        sys: Optional[str] = None,
        root: str = "/mp",
        workers: int = 8,
        max_capacity_gb: float = 0.0,
        verify_size: bool = True,
        status_interval_s: float = 0.0,
        task_workers: int = 32,
        load_schedule: str = "fifo",
        probe_chunks: int = 64,
    ):
        self.pool = pool
        self.container = container
        self.sys = sys
        self.root = root.rstrip("/") or "/"
        self.workers = workers
        self.max_capacity_gb = max_capacity_gb
        self.verify_size = verify_size
        self.status_interval_s = status_interval_s
        self.task_workers = task_workers
        self.load_schedule = load_schedule
        self.probe_chunks = probe_chunks

    @classmethod
    def from_dict(cls, d: dict) -> "DaosL2AdapterConfig":
        pool = d.get("pool")
        container = d.get("container")
        if not isinstance(pool, str) or not pool:
            raise ValueError("daos: 'pool' must be a non-empty string")
        if not isinstance(container, str) or not container:
            raise ValueError("daos: 'container' must be a non-empty string")
        sysname = d.get("sys")
        if sysname is not None and not isinstance(sysname, str):
            raise ValueError("daos: 'sys' must be a string")
        root = d.get("root", "/mp")
        if not isinstance(root, str) or not root.startswith("/"):
            raise ValueError("daos: 'root' must be an absolute DFS path")
        workers = d.get("workers", 8)
        if not isinstance(workers, int) or workers <= 0:
            raise ValueError("daos: 'workers' must be a positive integer")
        cap = d.get("max_capacity_gb", 0)
        if not isinstance(cap, (int, float)) or cap < 0:
            raise ValueError("daos: 'max_capacity_gb' must be >= 0")
        verify_size = d.get("verify_size", True)
        if not isinstance(verify_size, bool):
            raise ValueError("daos: 'verify_size' must be a boolean")
        status_interval = d.get("status_interval_s", 0)
        if not isinstance(status_interval, (int, float)) or status_interval < 0:
            raise ValueError("daos: 'status_interval_s' must be >= 0")
        task_workers = d.get("task_workers", 32)
        if not isinstance(task_workers, int) or task_workers <= 0:
            raise ValueError("daos: 'task_workers' must be a positive integer")
        load_schedule = d.get("load_schedule", "fifo")
        if load_schedule not in ("fifo", "fair"):
            raise ValueError("daos: 'load_schedule' must be 'fifo' or 'fair'")
        probe_chunks = d.get("probe_chunks", 64)
        if not isinstance(probe_chunks, int) or probe_chunks < 0:
            raise ValueError("daos: 'probe_chunks' must be a non-negative integer")
        cfg = cls(
            pool=pool,
            container=container,
            sys=sysname,
            root=root,
            workers=workers,
            max_capacity_gb=float(cap),
            verify_size=verify_size,
            status_interval_s=float(status_interval),
            task_workers=task_workers,
            load_schedule=load_schedule,
            probe_chunks=probe_chunks,
        )
        # Optional common sub-configs handled by the base class parsers.
        cfg.eviction_config = cls._parse_eviction_config(d)
        cfg.persist_config = cls._parse_persist_config(d)
        cfg.serde_config = cls._parse_serde_config(d)
        return cfg

    @classmethod
    def help(cls) -> str:
        return (
            "DAOS L2 adapter config fields:\n"
            "- pool (str): DAOS pool label or UUID (required)\n"
            "- container (str): POSIX container label or UUID (required)\n"
            "- sys (str): DAOS system name (optional)\n"
            "- root (str): DFS directory holding the objects "
            "(optional, default '/mp')\n"
            "- workers (int): I/O threads (optional, default 8)\n"
            "- load_schedule ('fifo'|'fair'): how concurrent load tasks share the I/O "
            "threads. fifo (default) minimises mean latency; fair interleaves keys "
            "across tasks so TTFT p95 approaches p50 under saturation\n"
            "- task_workers (int): concurrent tasks admitted (optional, default 32). "
            "Must exceed the number of requests the server keeps in flight, or "
            "the excess requests queue a whole task time behind the others\n"
            "- max_capacity_gb (number): declared capacity for global "
            "eviction; 0 = unbounded (optional)\n"
            "- verify_size (bool): treat size mismatches as misses "
            "(optional, default true)\n"
            "- status_interval_s (number): log report_status() this often "
            "while active; 0 disables (optional). The MP server's own "
            "--enable-extra-logging needs observability, which the image lacks\n"
            "- eviction / persist_enabled / serde: common L2 options"
        )


class DaosL2Adapter(L2AdapterInterface):
    def __init__(
        self,
        config: DaosL2AdapterConfig,
        dfs_factory: Optional[Callable[[], Any]] = None,
    ):
        super().__init__(max_capacity_bytes=int(config.max_capacity_gb * (1 << 30)))
        self._config = config
        self._root = config.root
        self._verify_size = config.verify_size

        factory = dfs_factory or (
            lambda: DfsSys(pool=config.pool, cont=config.container, sys=config.sys)
        )
        self._dfs = factory()
        if self._root != "/":
            self._dfs.mkdir_p(self._root)

        # Two pools on purpose. A task thread fans its keys out to the I/O
        # pool and waits for them; if both ran on one pool, N concurrent
        # tasks would occupy all N workers and wait forever for sub-work that
        # can never be scheduled (classic executor self-deadlock).
        #
        # The task pool must be *wider* than the server's request
        # concurrency: a task that cannot get a thread is not even submitted
        # to the I/O pool and pays a whole task duration extra. Measured on
        # client-5 (12 inflight, 8 task threads): a burst of 12 requests at
        # ~2x latency every run -- the p95 tail. Task threads are cheap
        # (they block on futures), so default to 32.
        self._tasks = concurrent.futures.ThreadPoolExecutor(
            max_workers=config.task_workers,
            thread_name_prefix="daos-l2-task",
        )
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=config.workers, thread_name_prefix="daos-l2-io"
        )
        # Metadata ops (stat for lookup, remove for delete) get their own
        # small pool: a 16-key lookup queued behind 640 MiB of bulk reads on
        # the I/O pool inflated request latency by a whole load time.
        self._meta = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(4, config.workers), thread_name_prefix="daos-l2-meta"
        )
        self._fair = (_FairDispatcher(self._pool, config.workers)
                      if config.load_schedule == "fair" else None)
        self._warm_up()

        self._store_efd = create_event_notifier()
        self._lookup_efd = create_event_notifier()
        self._load_efd = create_event_notifier()

        self._lock = threading.Lock()
        self._next_task_id: L2TaskId = 0
        self._completed_store: Dict[L2TaskId, L2StoreResult] = {}
        self._completed_lookup: Dict[L2TaskId, Bitmap] = {}
        self._completed_load: Dict[L2TaskId, Bitmap] = {}
        self._locks: Dict[ObjectKey, int] = {}
        self._inflight = 0
        self._stats = {
            "store_tasks": 0, "store_keys": 0, "store_skipped": 0,
            "store_failed_keys": 0, "lookup_tasks": 0, "lookup_hits": 0,
            "lookup_misses": 0, "load_tasks": 0, "load_ok": 0,
            "load_failed": 0, "deleted": 0, "errors": 0,
            "load_bytes": 0, "load_seconds": 0.0, "lookup_seconds": 0.0,
            "store_bytes": 0, "store_seconds": 0.0,
        }
        self._closing = False
        self._status_thread = None
        if config.status_interval_s > 0:
            self._status_thread = threading.Thread(
                target=self._status_loop, args=(config.status_interval_s,),
                name="daos-l2-status", daemon=True,
            )
            self._status_thread.start()
        logger.info(
            "DaosL2Adapter: pool=%s container=%s root=%s workers=%d "
            "capacity=%d verify_size=%s schedule=%s",
            config.pool, config.container, self._root, config.workers,
            self._max_capacity_bytes, self._verify_size, config.load_schedule,
        )

    def _warm_up(self) -> None:
        """Open a transport connection to EVERY target before serving.

        Root cause of the 14-17 s stalls seen on ``ucx+rc_v`` (MP-MODE-PLAN
        7.7e): mercury NA-UCX connects to a server xstream lazily, on the
        first RPC to it, through rdma_cm. When that first contact happens in
        the middle of a store -- the client->server direction is then saturated
        by the servers' RDMA reads on a lossy RoCE fabric -- the CM ``RTU``
        message is dropped, the server's endpoint stays half-open until the
        kernel CM retransmits ``REP`` (~16 s), and every RPC to that one
        rank:tag waits. The 16-chunk S16 probe used before covered only 12 of
        16 targets, so the last four were first contacted by user traffic.

        Fix: create the probe with object class ``SX`` (one shard on every
        target) and read ``probe_chunks`` chunks (>= target count), so all
        connections are established here, while the fabric is quiet and
        before the MP server accepts requests. The probe is per process and
        removed afterwards; ``probe_chunks: 0`` disables the warm-up.
        """
        nchunks = int(getattr(self._config, "probe_chunks", 64) or 0)
        if nchunks <= 0:
            return
        path = f"{self._root}/.daos-l2-probe.{os.getpid()}"
        t0 = time.monotonic()
        try:
            chunk = 4 << 20
            # Phase 1 -- connect: one tiny write per DFS chunk, sequentially.
            # Each chunk lives on a different shard/target (SX), so this is
            # the first RPC to every rank:tag, issued one at a time while the
            # fabric is quiet: the CM handshake is not competing with a bulk
            # stream (a 256 MiB parallel write here reproduced the lost-RTU
            # 16 s wait at start-up in 5 of 6 launches).
            tiny = 4096
            small = (ctypes.c_char * tiny).from_buffer(bytearray(b"\x5a" * tiny))
            h = self._dfs.open_rdwr_create(path, oclass=OC_SX)
            try:
                for i in range(nchunks):
                    self._dfs.write_obj_from(h, i * chunk, tiny, small)
                t1 = time.monotonic()
                # Phase 2 -- bandwidth warm-up on the now-connected endpoints:
                # a full 4 MiB write per chunk in parallel, then read back.
                src = (ctypes.c_char * chunk).from_buffer(bytearray(b"\xa5" * chunk))

                def _write(i):
                    return self._dfs.write_obj_from(h, i * chunk, chunk, src)

                list(self._pool.map(_write, range(nchunks)))
            finally:
                self._dfs.close_obj(h)

            def _read(i):
                dst = (ctypes.c_char * chunk).from_buffer(bytearray(chunk))
                hh = self._dfs.open_rdonly(path)
                try:
                    return self._dfs.read_obj_into(hh, i * chunk, chunk, dst)
                finally:
                    self._dfs.close_obj(hh)

            got = list(self._pool.map(_read, range(nchunks)))
            try:
                self._dfs.remove(path)
            except Exception as e:  # pragma: no cover
                logger.warning("DaosL2Adapter warm-up: could not remove %s: %s", path, e)
            logger.info("DaosL2Adapter warm-up: SX probe, %d targets contacted in %.1f ms, "
                        "%d chunk writes+reads of %d bytes (%s ok), total %.1f ms",
                        nchunks, (t1 - t0) * 1e3, len(got), chunk,
                        sum(1 for g in got if g == chunk), (time.monotonic() - t0) * 1e3)
        except Exception as e:  # pragma: no cover - best effort
            logger.warning("DaosL2Adapter warm-up failed (%s) after %.1f ms",
                           e, (time.monotonic() - t0) * 1e3)

    # -- event fds ----------------------------------------------------------
    def get_store_event_fd(self) -> int:
        return self._store_efd.fileno()

    def get_lookup_and_lock_event_fd(self) -> int:
        return self._lookup_efd.fileno()

    def get_load_event_fd(self) -> int:
        return self._load_efd.fileno()

    # -- helpers ------------------------------------------------------------
    def _path(self, key: ObjectKey) -> str:
        return f"{self._root}/{_key_to_name(key)}"

    def _new_task_id(self) -> L2TaskId:
        with self._lock:
            tid = self._next_task_id
            self._next_task_id += 1
            self._inflight += 1
            return tid

    def _finish(self) -> None:
        with self._lock:
            self._inflight -= 1

    @staticmethod
    def _cbuf(obj) :
        """ctypes view aliasing the MemoryObj payload (no copy)."""
        mv = obj.byte_array
        n = len(mv)
        return (ctypes.c_char * n).from_buffer(mv), n

    def _map(self, fn, items):
        """Run ``fn`` over ``items`` on the pool; return results in order."""
        return list(self._pool.map(fn, items))

    # -- store --------------------------------------------------------------
    def submit_store_task(
        self, keys: List[ObjectKey], objects: List["Any"]
    ) -> L2TaskId:
        tid = self._new_task_id()
        self._tasks.submit(self._execute_store, list(keys), list(objects), tid)
        return tid

    def _store_one(self, item) -> int:
        """Returns bytes written (>0), 0 if skipped (already present), -1 on error."""
        key, obj = item
        path = self._path(key)
        try:
            src, n = self._cbuf(obj)
            have = self._dfs.stat_size(path)
            if have == n:
                return 0
            h = self._dfs.open_rdwr_create(path)
            try:
                wrote = self._dfs.write_obj_from(h, 0, n, src)
            finally:
                self._dfs.close_obj(h)
            if wrote != n:
                logger.warning("daos store short write %s: %d/%d", path, wrote, n)
                self._dfs.remove(path)
                return -1
            return n
        except (DaosError, OSError, ValueError) as e:
            logger.warning("daos store failed %s: %s", path, e)
            return -1

    def _execute_store(self, keys, objects, tid: L2TaskId) -> None:
        t0 = time.monotonic()
        success = True
        total = 0
        stored_keys: List[ObjectKey] = []
        sizes: List[int] = []
        try:
            results = self._map(self._store_one, list(zip(keys, objects)))
            for key, r in zip(keys, results):
                if r < 0:
                    success = False
                    self._stats["store_failed_keys"] += 1
                elif r == 0:
                    self._stats["store_skipped"] += 1
                else:
                    total += r
                    stored_keys.append(key)
                    sizes.append(r)
        except Exception:
            logger.exception("daos store task %d failed", tid)
            success = False
            self._stats["errors"] += 1
        with self._lock:
            self._completed_store[tid] = L2StoreResult(success, total)
            self._stats["store_tasks"] += 1
            self._stats["store_keys"] += len(keys)
            self._stats["store_bytes"] += total
            self._stats["store_seconds"] += time.monotonic() - t0
        if stored_keys:
            self._notify_keys_stored(stored_keys, sizes)
        self._finish()
        self._store_efd.notify()

    def pop_completed_store_tasks(self) -> Dict[L2TaskId, L2StoreResult]:
        with self._lock:
            done = self._completed_store
            self._completed_store = {}
        return done

    # -- lookup and lock ----------------------------------------------------
    def submit_lookup_and_lock_task(
        self, keys: List[ObjectKey], layout_desc: MemoryLayoutDesc
    ) -> L2TaskId:
        tid = self._new_task_id()
        expected = _expected_bytes(layout_desc) if self._verify_size else None
        self._tasks.submit(self._execute_lookup, list(keys), expected, tid)
        return tid

    def _lookup_one(self, item) -> bool:
        key, expected = item
        path = self._path(key)
        try:
            size = self._dfs.stat_size(path)
        except (DaosError, OSError) as e:
            logger.warning("daos stat failed %s: %s", path, e)
            self._stats["errors"] += 1
            return False
        if size is None:
            return False
        if expected is not None and size != expected:
            return False
        return True

    def _execute_lookup(self, keys, expected, tid: L2TaskId) -> None:
        t0 = time.monotonic()
        bitmap = Bitmap(len(keys))
        try:
            hits = list(self._meta.map(self._lookup_one, [(k, expected) for k in keys]))
            with self._lock:
                for i, (key, hit) in enumerate(zip(keys, hits)):
                    if hit:
                        bitmap.set(i)
                        self._locks[key] = self._locks.get(key, 0) + 1
                        self._stats["lookup_hits"] += 1
                    else:
                        self._stats["lookup_misses"] += 1
        except Exception:
            logger.exception("daos lookup task %d failed", tid)
            self._stats["errors"] += 1
        el = time.monotonic() - t0
        with self._lock:
            self._completed_lookup[tid] = bitmap
            self._stats["lookup_tasks"] += 1
            self._stats["lookup_seconds"] += el
        if el > 0.05:
            logger.info("daos l2 lookup task %d: %d keys in %.1f ms", tid, len(keys), el * 1e3)
        self._finish()
        self._lookup_efd.notify()

    def query_lookup_and_lock_result(self, task_id: L2TaskId) -> Optional[Bitmap]:
        with self._lock:
            return self._completed_lookup.pop(task_id, None)

    def submit_unlock(self, keys: List[ObjectKey]) -> None:
        with self._lock:
            for key in keys:
                n = self._locks.get(key, 0) - 1
                if n <= 0:
                    self._locks.pop(key, None)
                else:
                    self._locks[key] = n

    # -- load ---------------------------------------------------------------
    def submit_load_task(
        self, keys: List[ObjectKey], objects: List["Any"]
    ) -> L2TaskId:
        tid = self._new_task_id()
        self._tasks.submit(self._execute_load, list(keys), list(objects), tid)
        return tid

    def _load_one(self, item) -> bool:
        key, obj = item
        path = self._path(key)
        try:
            dst, n = self._cbuf(obj)
            h = self._dfs.open_rdonly(path)
            try:
                got = self._dfs.read_obj_into(h, 0, n, dst)
            finally:
                self._dfs.close_obj(h)
            if got != n:
                logger.warning("daos load short read %s: %d/%d", path, got, n)
                return False
            return True
        except (DaosError, OSError, ValueError) as e:
            logger.warning("daos load failed %s: %s", path, e)
            return False

    def _execute_load(self, keys, objects, tid: L2TaskId) -> None:
        t0 = time.monotonic()
        nbytes = 0
        bitmap = Bitmap(len(keys))
        hit_keys: List[ObjectKey] = []
        try:
            items = list(zip(keys, objects))
            oks = (self._fair.run(self._load_one, items) if self._fair
                   else self._map(self._load_one, items))
            for i, (key, ok) in enumerate(zip(keys, oks)):
                if ok:
                    bitmap.set(i)
                    hit_keys.append(key)
                    self._stats["load_ok"] += 1
                    nbytes += len(objects[i].byte_array)
                else:
                    self._stats["load_failed"] += 1
        except Exception:
            logger.exception("daos load task %d failed", tid)
            self._stats["errors"] += 1
        el = time.monotonic() - t0
        with self._lock:
            self._completed_load[tid] = bitmap
            self._stats["load_tasks"] += 1
            self._stats["load_bytes"] += nbytes
            self._stats["load_seconds"] += el
        if nbytes:
            logger.info(
                "daos l2 load task %d: %d/%d keys, %.1f MiB in %.1f ms (%.2f GB/s)",
                tid, len(hit_keys), len(keys), nbytes / 2**20, el * 1e3,
                nbytes / el / 1e9 if el > 0 else 0.0,
            )
        if hit_keys:
            self._notify_keys_accessed(hit_keys)
        self._finish()
        self._load_efd.notify()

    def query_load_result(self, task_id: L2TaskId) -> Optional[Bitmap]:
        with self._lock:
            return self._completed_load.pop(task_id, None)

    # -- optional interface -------------------------------------------------
    def delete(self, keys: List[ObjectKey]) -> None:
        """Remove unlocked keys. Runs synchronously (eviction is rare and the
        controller expects the accounting update to be visible on return)."""
        with self._lock:
            todo = [k for k in keys if self._locks.get(k, 0) == 0]
        deleted: List[ObjectKey] = []
        sizes: List[int] = []
        for key in todo:
            path = self._path(key)
            try:
                size = self._dfs.stat_size(path)
                if size is None:
                    continue
                if self._dfs.remove(path):
                    deleted.append(key)
                    sizes.append(size)
            except (DaosError, OSError) as e:
                logger.warning("daos delete failed %s: %s", path, e)
                self._stats["errors"] += 1
        if deleted:
            self._stats["deleted"] += len(deleted)
            self._notify_keys_deleted(deleted, sizes)

    def report_status(self) -> dict:
        with self._lock:
            st = dict(self._stats)
            st.update(
                {
                    "type": ADAPTER_TYPE,
                    "pool": self._config.pool,
                    "container": self._config.container,
                    "root": self._root,
                    "workers": self._config.workers,
                    "inflight_tasks": self._inflight,
                    "locked_keys": len(self._locks),
                    "bytes_used": self._total_bytes_used,
                }
            )
        return st

    def _status_loop(self, interval: float) -> None:
        last = None
        while not self._closing:
            time.sleep(interval)
            st = self.report_status()
            sig = (st["store_tasks"], st["lookup_tasks"], st["load_tasks"], st["deleted"])
            if sig != last:
                last = sig
                logger.info("daos l2 status: %s", st)

    def close(self) -> None:
        self._closing = True
        # Let in-flight tasks drain (they hold MemoryObj buffers the caller
        # still owns), then release the pool and the DFS mount.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            with self._lock:
                if self._inflight == 0:
                    break
            time.sleep(0.05)
        if self._fair:
            self._fair.close()
        self._tasks.shutdown(wait=True)
        self._pool.shutdown(wait=True)
        self._meta.shutdown(wait=True)
        try:
            self._dfs.close()
        except Exception:  # pragma: no cover
            logger.exception("daos close failed")
        for efd in (self._store_efd, self._lookup_efd, self._load_efd):
            try:
                efd.close()
            except Exception:  # pragma: no cover
                pass


def _factory(config, l1_memory_desc=None):
    if not isinstance(config, DaosL2AdapterConfig):
        raise TypeError(f"daos factory got {type(config).__name__}")
    return DaosL2Adapter(config)


def register() -> None:
    """Idempotent registration of the 'daos' L2 adapter type."""
    try:
        register_l2_adapter_type(ADAPTER_TYPE, DaosL2AdapterConfig)
    except ValueError:
        return  # already registered
    register_l2_adapter_factory(ADAPTER_TYPE, _factory)


register()
