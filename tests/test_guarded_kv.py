#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""GuardedKV (the MP adapter's storage over the rank guard), without DAOS.

The store is a dict that can be told a key's rank is down: requests then end
the way S1BucketStore ends them on a dead rank (DEADLINE, the buffers kept by
the caller's zombie) and are recorded, so the test can check that L1's buffer
was never handed to DAOS.

    python3 tests/test_guarded_kv.py
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmcache_daos import rank_guard as G                 # noqa: E402
from lmcache_daos.mp.guarded_kv import AKEY_DATA, AKEY_LEN, GuardedKV  # noqa: E402

fails = 0


def ck(what, ok, extra=""):
    global fails
    if not ok:
        fails += 1
    print("  %-62s %s %s" % (what, "PASS" if ok else "FAIL", extra))


class DictStore:
    """S1BucketStore's call shape over a dict; ``down`` makes every request a DEADLINE."""

    def __init__(self):
        self.data = {}
        self.down = False
        self.error = None
        self.seen = []                   # addresses of every buffer handed to "DAOS"
        self.parked = []                 # what a zombie would keep alive

    def _addr(self, buf):
        return ctypes.addressof(buf)

    def fetch(self, dkey, items, single=False, on_reap=None):
        self.seen += [self._addr(b) for _, b, _ in items]
        if self.down:
            self.parked.append(list(items))
            return G.DEADLINE, None
        got = []
        for ak, buf, n in items:
            v = self.data.get((dkey, ak))
            if v is None:
                got.append(0)
            else:
                ctypes.memmove(buf, v, min(n, len(v)))
                got.append(min(n, len(v)))
        return 0, got

    def update(self, dkey, items, single=False):
        self.seen += [self._addr(b) for _, b, _ in items]
        if self.down:
            self.parked.append(list(items))
            return G.DEADLINE
        for ak, buf, n in items:
            self.data[(dkey, ak)] = ctypes.string_at(buf, n)
        return 0

    def punch(self, dkey, akeys=None):
        if self.down:
            return G.DEADLINE
        for k in [k for k in self.data if k[0] == dkey]:
            del self.data[k]
        return 0

    def close(self):
        pass


def roundtrip():
    st = DictStore()
    kv = GuardedKV(st)
    src = ctypes.create_string_buffer(b"x" * 1000, 1000)
    ck("store: returns the length", kv.write_obj_from(kv.open_rdwr_create("/a"), 0, 1000, src) == 1000)
    ck("stat: committed length", kv.stat_size("/a") == 1000)
    dst = ctypes.create_string_buffer(1000)
    n = kv.read_obj_into(kv.open_rdonly("/a"), 0, 1000, dst)
    ck("load: bytes and content", n == 1000 and dst.raw == b"x" * 1000)
    ck("stat: absent key is None", kv.stat_size("/nope") is None)
    ck("load: absent key reads 0", kv.read_obj_into("/nope", 0, 10, ctypes.create_string_buffer(10)) == 0)
    ck("load: wrong size reads 0", kv.read_obj_into("/a", 0, 999, ctypes.create_string_buffer(999)) == 0)
    ck("remove: punches", kv.remove("/a") and kv.stat_size("/a") is None)


def uncommitted_is_absent():
    st = DictStore()
    kv = GuardedKV(st)
    dk = kv._dkey("/b")
    st.data[(dk, AKEY_DATA)] = b"y" * 10       # payload landed, length never did
    ck("commit: payload without length reads as absent", kv.stat_size("/b") is None)
    ck("commit: and loads 0", kv.read_obj_into("/b", 0, 10, ctypes.create_string_buffer(10)) == 0)


def l1_buffers_never_reach_daos():
    st = DictStore()
    kv = GuardedKV(st)
    # both allocated first: a freed private copy's address could otherwise be
    # handed to dst and look like dst itself
    src = ctypes.create_string_buffer(b"z" * 64, 64)
    dst = ctypes.create_string_buffer(64)
    kv.write_obj_from("/c", 0, 64, src)
    kv.read_obj_into("/c", 0, 64, dst)
    ck("copy: neither the L1 source nor destination went to DAOS",
       ctypes.addressof(src) not in st.seen and ctypes.addressof(dst) not in st.seen)


def dead_rank():
    st = DictStore()
    kv = GuardedKV(st)
    kv.write_obj_from("/d", 0, 32, ctypes.create_string_buffer(b"w" * 32, 32))
    st.down = True
    dst = ctypes.create_string_buffer(b"L" * 32, 32)
    ck("down: stat is a miss, not an error", kv.stat_size("/d") is None)
    # the length fetch already missed; force the payload path too
    st.down = False
    have = kv.stat_size("/d")
    st.down = True
    orig = kv.stat_size
    kv.stat_size = lambda p: have
    n = kv.read_obj_into("/d", 0, 32, dst)
    kv.stat_size = orig
    ck("down: load past its deadline reads 0", n == 0)
    ck("down: L1 destination untouched", dst.raw == b"L" * 32)
    ck("down: the zombie keeps a private buffer, not L1's",
       st.parked and all(ctypes.addressof(b) != ctypes.addressof(dst) for _, b, _ in st.parked[-1]))
    src = ctypes.create_string_buffer(b"q" * 32, 32)
    ck("down: store is dropped, returns 0", kv.write_obj_from("/e", 0, 32, src) == 0)
    ck("down: remove fails softly", kv.remove("/d") is False)
    st.down = False
    ck("up again: the earlier key still reads", kv.stat_size("/d") == 32)


def bounce_reuse():
    """Completed ops recycle their private buffer; a zombie's is never reused."""
    st = DictStore()
    kv = GuardedKV(st)
    src = ctypes.create_string_buffer(b"r" * 128, 128)
    kv.write_obj_from("/f", 0, 128, src)
    w = st.seen[0]                       # the write's payload buffer
    dst = ctypes.create_string_buffer(128)
    st.seen = []
    kv.read_obj_into("/f", 0, 128, dst)
    kv.read_obj_into("/f", 0, 128, dst)
    ck("reuse: the write and both reads used one pooled buffer",
       st.seen[1] == w and st.seen[3] == w and kv._bounce.idle_bytes() == 128,
       "idle=%d" % kv._bounce.idle_bytes())
    have = kv.stat_size("/f")
    kv.stat_size = lambda p: have
    st.down = True
    seen0 = len(st.seen)
    kv.read_obj_into("/f", 0, 128, dst)
    zombie = st.seen[seen0]
    ck("reuse: a timed-out read keeps its buffer out of the pool",
       kv._bounce.idle_bytes() == 0)
    st.down = False
    st.seen = []
    kv.read_obj_into("/f", 0, 128, dst)
    ck("reuse: the next read does not get the zombie's buffer",
       zombie not in st.seen and dst.raw == b"r" * 128)
    kv.write_obj_from("/g", 0, 128, src)
    st.down = True
    n = kv.write_obj_from("/h", 0, 128, src)
    ck("reuse: a dropped write keeps its buffer out of the pool",
       n == 0 and kv._bounce.idle_bytes() == 0)


def main():
    for t in (roundtrip, uncommitted_is_absent, l1_buffers_never_reach_daos, dead_rank,
              bounce_reuse):
        print(t.__name__)
        t()
    print("FAIL" if fails else "OK", "(%d failed)" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
