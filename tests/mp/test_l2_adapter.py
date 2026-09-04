"""Unit test for the MP-mode DAOS L2 adapter against an in-memory fake DFS.

Runs anywhere LMCache is importable (no DAOS needed):

    PYTHONPATH=/lmd python3 /lmd/tests/mp/test_l2_adapter.py
    PYTHONPATH=/lmd python3 -m pytest /lmd/tests/mp -q

What it proves:
  * store -> lookup(hit, locked) -> load returns the same bytes, zero-copy
    into the caller's buffer;
  * a missing key and a torn (short) object are misses, not errors;
  * store is idempotent (second store skipped, bytes not double counted);
  * a locked key survives delete(), an unlocked one does not, and the byte
    accounting goes back to zero;
  * every task signals its eventfd exactly once and results are popped once.
"""

from __future__ import annotations

import ctypes
import os
import select
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import torch  # noqa: E402

from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey  # noqa: E402

from lmcache_daos.mp.l2_adapter import (  # noqa: E402
    DaosL2Adapter,
    DaosL2AdapterConfig,
    _key_to_name,
)


class FakeDfs:
    """Dict-backed stand-in for DfsSys exposing only what the adapter uses."""

    def __init__(self):
        self.files = {}
        self.dirs = set()
        self.lock = threading.Lock()

    def mkdir_p(self, path):
        self.dirs.add(path)

    def stat_size(self, path):
        with self.lock:
            b = self.files.get(path)
        return None if b is None else len(b)

    def open_rdwr_create(self, path):
        with self.lock:
            self.files.setdefault(path, bytearray())
        return path

    def open_rdonly(self, path):
        with self.lock:
            if path not in self.files:
                raise OSError(2, "ENOENT", path)
        return path

    def close_obj(self, h):
        pass

    def write_obj_from(self, h, offset, length, src):
        data = ctypes.string_at(src, length)
        with self.lock:
            buf = self.files[h]
            buf[offset:offset + length] = data
        return length

    def read_obj_into(self, h, offset, length, dest):
        with self.lock:
            data = bytes(self.files[h][offset:offset + length])
        ctypes.memmove(dest, data, len(data))
        return len(data)

    def remove(self, path):
        with self.lock:
            return self.files.pop(path, None) is not None

    def close(self):
        pass


class FakeObj:
    def __init__(self, n, fill=None):
        self.buf = bytearray(n) if fill is None else bytearray(fill)

    @property
    def byte_array(self):
        return memoryview(self.buf)


def key(i, salt=""):
    return ObjectKey(
        chunk_hash=bytes([i]) * 32,
        model_name="Qwen/Qwen3-14B",
        kv_rank=0,
        object_group_id=0,
        cache_salt=salt,
    )


def wait_fd(fd, timeout=5.0):
    r, _, _ = select.select([fd], [], [], timeout)
    assert r, "eventfd not signalled"
    os.read(fd, 8)  # drain the counter as the L2 controller would


def wait_result(fn, tid, fd):
    wait_fd(fd)
    res = fn(tid)
    assert res is not None
    assert fn(tid) is None, "result must be returned exactly once"
    return res


def make():
    cfg = DaosL2AdapterConfig.from_dict(
        {"type": "daos", "pool": "p", "container": "c", "root": "/mp",
         "workers": 4, "max_capacity_gb": 1}
    )
    dfs = FakeDfs()
    ad = DaosL2Adapter(cfg, dfs_factory=lambda: dfs)
    return ad, dfs


def test_key_name():
    k = key(1, salt="tenant-a")
    n = _key_to_name(k)
    assert n.startswith("Qwen_Qwen3-14B@00000000@0@" + ("01" * 32))
    assert n.endswith("@tenant-a")
    assert "/" not in n


def test_store_lookup_load_roundtrip():
    ad, dfs = make()
    n = 4096
    keys = [key(1), key(2)]
    objs = [FakeObj(n, bytes([0xA1]) * n), FakeObj(n, bytes([0xB2]) * n)]
    tid = ad.submit_store_task(keys, objs)
    wait_fd(ad.get_store_event_fd())
    done = ad.pop_completed_store_tasks()
    assert set(done) == {tid} and done[tid].is_successful()
    assert done[tid].bytes_transferred() == 2 * n
    assert ad.pop_completed_store_tasks() == {}
    assert ad.get_usage().total_bytes_used == 2 * n
    assert len(dfs.files) == 2 and all(p.startswith("/mp/") for p in dfs.files)

    layout = MemoryLayoutDesc(shapes=[torch.Size([n // 2])], dtypes=[torch.float16])
    tid = ad.submit_lookup_and_lock_task(keys + [key(9)], layout)
    bm = wait_result(ad.query_lookup_and_lock_result, tid, ad.get_lookup_and_lock_event_fd())
    assert bm.test(0) and bm.test(1) and not bm.test(2)

    dst = [FakeObj(n), FakeObj(n), FakeObj(n)]
    tid = ad.submit_load_task(keys + [key(9)], dst)
    bm = wait_result(ad.query_load_result, tid, ad.get_load_event_fd())
    assert bm.test(0) and bm.test(1) and not bm.test(2)
    assert bytes(dst[0].buf) == bytes([0xA1]) * n
    assert bytes(dst[1].buf) == bytes([0xB2]) * n

    # locked by the lookup: delete must skip; after unlock it must remove.
    ad.delete(keys)
    assert len(dfs.files) == 2
    ad.submit_unlock(keys)
    ad.delete(keys)
    assert len(dfs.files) == 0
    assert ad.get_usage().total_bytes_used == 0
    st = ad.report_status()
    assert st["deleted"] == 2 and st["load_ok"] == 2 and st["load_failed"] == 1
    ad.close()


def test_idempotent_store_and_size_check():
    ad, dfs = make()
    n = 2048
    k = key(3)
    tid = ad.submit_store_task([k], [FakeObj(n, b"x" * n)])
    wait_fd(ad.get_store_event_fd())
    assert ad.pop_completed_store_tasks()[tid].bytes_transferred() == n
    tid = ad.submit_store_task([k], [FakeObj(n, b"x" * n)])
    wait_fd(ad.get_store_event_fd())
    r = ad.pop_completed_store_tasks()[tid]
    assert r.is_successful() and r.bytes_transferred() == 0
    assert ad.get_usage().total_bytes_used == n
    assert ad.report_status()["store_skipped"] == 1

    # Torn object: truncate behind the adapter's back -> lookup with the
    # expected layout misses, and a load into a full-size buffer fails.
    path = next(iter(dfs.files))
    del dfs.files[path][n // 2:]
    layout = MemoryLayoutDesc(shapes=[torch.Size([n])], dtypes=[torch.uint8])
    tid = ad.submit_lookup_and_lock_task([k], layout)
    bm = wait_result(ad.query_lookup_and_lock_result, tid, ad.get_lookup_and_lock_event_fd())
    assert not bm.test(0)
    tid = ad.submit_load_task([k], [FakeObj(n)])
    bm = wait_result(ad.query_load_result, tid, ad.get_load_event_fd())
    assert not bm.test(0)
    ad.close()


def test_concurrent_tasks_complete_once_each():
    ad, _ = make()
    n = 1024
    tids = [ad.submit_store_task([key(10 + i)], [FakeObj(n, bytes([i]) * n)])
            for i in range(16)]
    seen = {}
    fd = ad.get_store_event_fd()
    while len(seen) < 16:
        wait_fd(fd)
        seen.update(ad.pop_completed_store_tasks())
    assert set(seen) == set(tids)
    assert all(r.is_successful() and r.bytes_transferred() == n for r in seen.values())
    assert ad.report_status()["inflight_tasks"] == 0
    ad.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASS")
