# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""PROTOTYPE: overlap L2 (DAOS) loading with the L1->GPU transfer in MP mode.

Why this exists
---------------
In LMCache 0.5.2's MP server a request's L2 keys are loaded as ONE task per
adapter, the prefetch is reported complete only when that task is done, vLLM
schedules the request after that, and only then does the worker's RETRIEVE
copy L1->GPU. The two big stages are strictly serial, so a cold hit costs
``load + transfer`` (measured: 16K 250 ms = 105 load + ~130 transfer; 127K
1182 = 634 + ~550). This module makes them overlap without touching the
installed LMCache files, by monkey-patching four seams from our server entry
(``DAOS_MP_STREAM=<sub-batch keys>``):

1. **L2 adapter proxy** -- ``submit_load_task`` is split into sub-batches of
   *N* keys, submitted in order. As each sub-batch completes, its keys are
   moved to the readable state right away (``finish_write_and_reserve_read``),
   instead of waiting for the whole request.
2. **Prefetch status** -- ``wait/query_prefetch_status`` report the request
   as done as soon as the *lookup* phase has decided the hit count (the L2
   objects are known to exist), so vLLM schedules the request while the
   loads are still running.
3. **read_prefetched_results** -- keys that have not landed yet are returned
   as ``_Pending`` placeholders instead of failing the retrieve.
4. **transfer_kv_per_object_group** (H2D) -- transfers sub-batch by
   sub-batch, waiting for each placeholder to become readable first. The
   first sub-batch starts copying while later ones are still being read from
   DAOS.

Known limitations (prototype, not for production):
- early ``finish_write_and_reserve_read`` uses ``extra_count=0`` (TP=1 only);
- a key whose load later fails makes the retrieve fail (vLLM then applies its
  kv-load failure policy) instead of being reported as a miss up front;
- the real prefetch result is drained by a janitor thread to avoid leaks.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any, Dict, List, Set

from lmcache.logging import init_logger
from lmcache.native_storage_ops import Bitmap

logger = init_logger(__name__)

_state: Dict[str, Any] = {"l1": None, "pc": None, "sub": 8, "applied": False}
_early_lock = threading.Lock()
_early: Set[Any] = set()          # keys already transitioned to readable by the proxy
_ready_cv = threading.Condition()
_ready: Set[Any] = set()          # keys made readable early; waiters are notified
_drain_lock = threading.Lock()
_drain: Set[int] = set()          # prefetch request ids whose real result must be popped


class _Pending:
    """Placeholder for a key that has not landed in L1 yet."""

    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key

    def get_size(self) -> int:  # retrieve() sums sizes before transferring
        return 0


class StreamingL2Proxy:
    """Wraps an L2AdapterInterface; splits loads and publishes early."""

    def __init__(self, inner, sub: int):
        self._inner = inner
        self._sub = max(1, sub)
        self._lock = threading.Lock()
        self._next = 1 << 40           # keep clear of the inner task ids
        self._outer: Dict[int, Dict[str, Any]] = {}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def submit_load_task(self, keys: List[Any], objects: List[Any]) -> int:
        subs = []
        for i in range(0, len(keys), self._sub):
            ks, os_ = list(keys[i:i + self._sub]), list(objects[i:i + self._sub])
            subs.append({"tid": self._inner.submit_load_task(ks, os_), "keys": ks, "bm": None})
        with self._lock:
            outer = self._next
            self._next += 1
            self._outer[outer] = {"subs": subs, "n": len(keys)}
        return outer

    def query_load_result(self, task_id: int):
        with self._lock:
            st = self._outer.get(task_id)
        if st is None:
            return self._inner.query_load_result(task_id)
        l1 = _state["l1"]
        for s in st["subs"]:
            if s["bm"] is not None:
                continue
            bm = self._inner.query_load_result(s["tid"])
            if bm is None:
                continue
            s["bm"] = bm
            ok = [k for i, k in enumerate(s["keys"]) if bm.test(i)]
            if ok and l1 is not None:
                with _early_lock:
                    _early.update(ok)
                _orig_fwrr(l1, ok)          # readable now; retrieve can start on them
                with _ready_cv:
                    _ready.update(ok)
                    _ready_cv.notify_all()
        if any(s["bm"] is None for s in st["subs"]):
            return None
        out = Bitmap(st["n"])
        pos = 0
        for s in st["subs"]:
            for i in range(len(s["keys"])):
                if s["bm"].test(i):
                    out.set(pos + i)
            pos += len(s["keys"])
        with self._lock:
            self._outer.pop(task_id, None)
        return out


_orig_fwrr = None


def _patched_fwrr(self, keys, extra_count=0):
    """Skip keys the proxy already transitioned (idempotent finish)."""
    with _early_lock:
        skip = [k for k in keys if k in _early]
        for k in skip:
            _early.discard(k)
    rest = [k for k in keys if k not in set(skip)]
    return _orig_fwrr(self, rest, extra_count) if rest else {}


def _janitor():
    pc_get = lambda: _state["pc"]  # noqa: E731
    while True:
        time.sleep(0.5)
        pc = pc_get()
        if pc is None:
            continue
        with _drain_lock:
            rids = list(_drain)
        for rid in rids:
            try:
                if pc.query_prefetch_result(rid) is not None:
                    with _drain_lock:
                        _drain.discard(rid)
            except Exception:  # pragma: no cover
                pass


def apply(sub_batch: int = 8) -> None:
    """Install the patches. Idempotent."""
    global _orig_fwrr
    if _state["applied"]:
        return
    _state["applied"] = True
    _state["sub"] = max(1, int(sub_batch))
    sub = _state["sub"]

    from lmcache.v1.distributed import l1_manager as l1m
    from lmcache.v1.distributed import storage_manager as smm
    from lmcache.v1.multiprocess.modules import lmcache_driven_transfer as trm

    # -- 1. adapter proxy + capture of the L1 manager / prefetch controller --
    orig_create = smm.create_l2_adapter
    smm.create_l2_adapter = lambda cfg, desc=None: StreamingL2Proxy(orig_create(cfg, desc), sub)

    orig_init = smm.StorageManager.__init__

    def _init(self, *a, **kw):
        orig_init(self, *a, **kw)
        _state["l1"] = self._l1_manager
        _state["pc"] = self._prefetch_controller

    smm.StorageManager.__init__ = _init

    _orig_fwrr = l1m.L1Manager.finish_write_and_reserve_read
    l1m.L1Manager.finish_write_and_reserve_read = _patched_fwrr

    # -- 2. prefetch status: done as soon as the lookup decided the hits -----
    orig_wait = smm.StorageManager.wait_prefetch_status
    orig_query = smm.StorageManager.query_prefetch_status

    def _wait(self, handle, timeout):
        rid = handle.prefetch_request_id
        if rid == -1:
            return orig_wait(self, handle, timeout)
        pc = self._prefetch_controller
        deadline = time.monotonic() + timeout
        while True:
            if pc.query_lookup_result(rid) is not None:
                return True
            if pc.wait_prefetch_result(rid, 0.002):
                return True
            if time.monotonic() >= deadline:
                return False

    def _query(self, handle):
        rid = handle.prefetch_request_id
        if rid == -1:
            return orig_query(self, handle)
        pc = self._prefetch_controller
        hits = pc.query_lookup_result(rid)
        if hits is None:
            return orig_query(self, handle)          # real result may already be in
        n_l2 = len(handle.l2_orig_indices)
        bm = Bitmap(n_l2)
        for i in range(min(hits, n_l2)):
            bm.set(i)
        with _drain_lock:
            _drain.add(rid)
        return self._combine_found(handle, bm)

    smm.StorageManager.wait_prefetch_status = _wait
    smm.StorageManager.query_prefetch_status = _query

    # -- 3. read_prefetched_results tolerates keys still in flight -----------
    @contextlib.contextmanager
    def _read(self, keys):
        res = self._l1_manager.unsafe_read(keys)
        objs = []
        for k in keys:
            e, o = res.get(k, (None, None))
            objs.append(o if o is not None else _Pending(k))
        yield objs

    smm.StorageManager.read_prefetched_results = _read

    # -- 4. H2D transfer in sub-batches, waiting per placeholder -------------
    orig_transfer = trm.transfer_kv_per_object_group

    def _resolve(pending: List[_Pending], timeout: float = 60.0):
        """Block until every pending key is readable in L1 (no polling: the
        proxy notifies ``_ready_cv`` as sub-batches land)."""
        l1 = _state["l1"]
        keys = [p.key for p in pending]
        deadline = time.monotonic() + timeout
        with _ready_cv:
            while True:
                res = l1.unsafe_read(keys)
                if all(res.get(k, (None, None))[1] is not None for k in keys):
                    for k in keys:
                        _ready.discard(k)
                    return {k: res[k][1] for k in keys}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = sum(1 for k in keys if res.get(k, (None, None))[1] is None)
                    raise RuntimeError(f"streaming: {missing} keys never landed in L1")
                _ready_cv.wait(min(remaining, 0.05))

    def _transfer(cache_context, block_ids_gpu, memory_objs, object_group_id,
                  batch_size, skip_first_n_tokens, direction):
        n = len(memory_objs)
        if n == 0 or not any(isinstance(m, _Pending) for m in memory_objs) \
                or direction != trm.lmc_ops.TransferDirection.H2D:
            return orig_transfer(cache_context, block_ids_gpu, memory_objs, object_group_id,
                                 batch_size, skip_first_n_tokens, direction)
        bpc = [len(b) // n for b in block_ids_gpu]
        t0 = time.perf_counter()
        waited = 0.0
        for i in range(0, n, sub):
            part = list(memory_objs[i:i + sub])
            pend = [m for m in part if isinstance(m, _Pending)]
            if pend:
                tw = time.perf_counter()
                got = _resolve(pend)
                waited += time.perf_counter() - tw
                part = [got[m.key] if isinstance(m, _Pending) else m for m in part]
            blocks = [b[i * c:(i + len(part)) * c] for b, c in zip(block_ids_gpu, bpc)]
            orig_transfer(cache_context, blocks, part, object_group_id, batch_size,
                          skip_first_n_tokens if i == 0 else 0, direction)
        logger.info("streaming H2D: %d chunks in %d sub-batches, %.1f ms total, %.1f ms waiting on L2",
                    n, (n + sub - 1) // sub, (time.perf_counter() - t0) * 1e3, waited * 1e3)

    trm.transfer_kv_per_object_group = _transfer

    threading.Thread(target=_janitor, name="daos-mp-stream-janitor", daemon=True).start()
    logger.info("MP streaming prototype enabled: sub-batch=%d keys", sub)
