#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""A torn dkey/akey object reads back as a miss, and gives its buffer back.

The same gate tests/test_torn_object.py holds for the DFS layout, re-proved for
the placement described in serde_v3 -- because the failure shapes are different
even though the policy is the same. In a file, torn means short. Under
dkey/akey it means an akey that is absent, and DAOS reports that with rc 0 and
an untouched iov_len, so "absent" and "a full buffer of uninitialised memory"
arrive looking identical unless the code checks the right field.

This drives the REAL connector rather than a copy of its read path. The DFS
version re-implements _get_sync because importing the connector needs LMCache;
that duplicate has to be kept in lockstep by hand, and a read path that drifts
from the one in production proves nothing. LMCache is present wherever this can
run at all, since it needs a live container too.

Damage is injected at the storage layer, not by constructing headers: write a
real object through the connector, then punch or overwrite akeys underneath it.
That is what a crashed writer actually leaves behind.

    DAOS_TEST_POOL=kvpool DAOS_TEST_CONT=nixltest python3 tests/test_torn_object_raw.py

The container must NOT be POSIX -- the connector would choose the DFS backend
and this would silently test the wrong thing, so it checks and refuses.
"""
import asyncio
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = 0


def ck(what, ok, extra=""):
    global fails
    if not ok:
        fails += 1
    print("  %-52s %s %s" % (what, "PASS" if ok else "FAIL", extra))


def main():
    pool = os.environ.get("DAOS_TEST_POOL", "kvpool")
    cont = os.environ.get("DAOS_TEST_CONT", "nixltest")

    import torch
    from lmcache.utils import CacheEngineKey
    from lmcache.v1.config import LMCacheEngineConfig
    from lmcache.v1.memory_management import MemoryFormat
    from lmcache.v1.metadata import LMCacheMetadata
    from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend

    from lmcache_daos import serde_v3 as v3
    from lmcache_daos.connector import DaosConnector

    cfg = LMCacheEngineConfig.from_defaults()
    cfg.remote_url = "daos://%s/%s" % (pool, cont)
    cfg.local_cpu = False
    meta = LMCacheMetadata(model_name="torn-raw", world_size=1, local_world_size=1,
                           worker_id=0, local_worker_id=0, kv_dtype=torch.bfloat16,
                           kv_shape=(2, 256, 8, 1, 8))
    lcb = LocalCPUBackend(cfg, meta)
    loop = asyncio.new_event_loop()
    conn = DaosConnector("daos://%s/%s" % (pool, cont), loop, lcb)

    if not conn._raw:
        print("  %s/%s is a POSIX container -- this test needs a non-POSIX one"
              % (pool, cont))
        return 2

    # Count allocations and hand-backs. A torn object that keeps its staging
    # buffer leaks one per miss, which on a low-hit-rate fleet is the whole
    # pool; the DFS test checks the same thing with its stub allocator.
    stats = {"alloc": 0, "release": 0}
    real_alloc, real_release = lcb.allocate, conn._release

    def counting_alloc(*a, **k):
        o = real_alloc(*a, **k)
        if o is not None:
            stats["alloc"] += 1
        return o

    def counting_release(mo):
        stats["release"] += 1
        return real_release(mo)

    lcb.allocate = counting_alloc
    conn._release = counting_release

    SZ = 1 << 20
    key = CacheEngineKey("torn-raw", 1, 0, 0x70A5, torch.bfloat16)
    dkey = v3.dkey_for(key)

    def store():
        """A real, complete object, written the way the connector writes one."""
        conn._obj.punch(dkey)
        mo = lcb.allocate(torch.Size([2, SZ // 4]), torch.bfloat16,
                          MemoryFormat.KV_2LTD)
        assert mo is not None, "allocate returned None -- staging pool too small"
        v = mo.byte_array
        v = v.cast("B") if isinstance(v, memoryview) else memoryview(v).cast("B")
        v[:len(v)] = bytes((i * 7 + 3) & 0xFF for i in range(256)) * (len(v) // 256)
        loop.run_until_complete(conn.put(key, mo))
        return len(v)

    def get():
        return loop.run_until_complete(conn.get(key))

    def meta_bytes():
        buf = ctypes.create_string_buffer(4096)
        (n,) = conn._obj.fetch(dkey, [(v3.AKEY_META, buf, 4096)], single=True)
        return bytes(buf[:n])

    def put_meta(raw: bytes):
        b = (ctypes.c_char * len(raw)).from_buffer_copy(raw)
        conn._obj.update(dkey, [(v3.AKEY_META, b, len(raw))], single=True)

    # -- 0. the control: an intact object is a hit ------------------------
    n = store()
    got = get()
    ck("intact object is a hit", got is not None)
    if got is not None:
        conn._release(got)

    # -- 1. nothing there at all ------------------------------------------
    conn._obj.punch(dkey)
    before = dict(stats)
    ck("absent dkey -> miss", get() is None)
    ck("  ...and allocated nothing",
       stats["alloc"] == before["alloc"], "(+%d)" % (stats["alloc"] - before["alloc"]))

    # -- 2. the crash this ordering exists to survive ---------------------
    # Payload written, metadata not: exactly the window between the two updates
    # in _put_raw. This is THE case serde_v3's write order is designed around.
    store()
    hdr = meta_bytes()
    conn._obj.punch(dkey)
    payload = ctypes.create_string_buffer(b"\xcd" * n, n)
    conn._obj.update(dkey, [(v3.AKEY_PAYLOAD, payload, n)])
    ck("payload written, metadata missing -> miss", get() is None)

    # -- 3..6. the metadata akey is damaged --------------------------------
    for name, mangle in (
        ("metadata truncated to the fixed part", lambda h: h[:v3.FIXED_SIZE]),
        ("metadata truncated mid-field", lambda h: h[:12]),
        ("metadata one byte short", lambda h: h[:-1]),
    ):
        store()
        before = dict(stats)
        put_meta(mangle(hdr))
        ck(name + " -> miss", get() is None)
        ck("  ...and allocated nothing",
           stats["alloc"] == before["alloc"])

    store()
    corrupt = bytearray(meta_bytes())
    corrupt[v3.FIXED_SIZE + 2] ^= 0xFF
    put_meta(bytes(corrupt))
    ck("metadata byte flipped (CRC) -> miss", get() is None)

    store()
    good = v3.parse_meta(meta_bytes())
    put_meta(v3.pack_meta(good.meta, good.payload_len, state=v3.STATE_WRITING))
    ck("metadata present but uncommitted -> miss", get() is None)

    # -- 7. metadata intact, payload gone ----------------------------------
    # Cannot happen with _put_raw's ordering, which is the point of checking:
    # a reader must not depend on the writer having been correct.
    store()
    hdr_now = meta_bytes()
    conn._obj.punch(dkey)
    put_meta(hdr_now)
    before = dict(stats)
    ck("metadata intact, payload absent -> miss", get() is None)
    ck("  ...and the staging buffer went back",
       stats["release"] - before["release"] == stats["alloc"] - before["alloc"],
       "(alloc +%d, release +%d)" % (stats["alloc"] - before["alloc"],
                                     stats["release"] - before["release"]))

    # -- 8. metadata intact, payload short ---------------------------------
    store()
    hdr_now = meta_bytes()
    conn._obj.punch(dkey)
    half = ctypes.create_string_buffer(b"\xee" * (n // 2), n // 2)
    conn._obj.update(dkey, [(v3.AKEY_PAYLOAD, half, n // 2)])
    put_meta(hdr_now)
    before = dict(stats)
    ck("payload half the promised length -> miss", get() is None)
    ck("  ...and the staging buffer went back",
       stats["release"] - before["release"] == stats["alloc"] - before["alloc"],
       "(alloc +%d, release +%d)" % (stats["alloc"] - before["alloc"],
                                     stats["release"] - before["release"]))

    # -- 9. still usable afterwards ----------------------------------------
    store()
    got = get()
    ck("a fresh store still reads back after all that", got is not None)
    if got is not None:
        conn._release(got)

    conn._obj.punch(dkey)
    loop.run_until_complete(conn.close())
    print("\n  === %s (%d failure%s) ===" % ("ALL PASS" if not fails else "FAILED",
                                             fails, "" if fails == 1 else "s"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
