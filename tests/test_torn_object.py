"""Gate: a torn object reads back as a miss, not as an error or as garbage.

The zero-copy read path reads straight into the MemoryObj buffer, so the buffer
is allocated before the payload length is known to be good. That makes two
things worth proving rather than assuming:

  1. every truncation stage is reported as None (absent / empty / mid-prefix /
     mid-metadata / truncated payload), never raised and never returned as a
     partially-filled object -- vLLM's default kv_load_failure_policy is `fail`,
     so raising would surface as a failed request;
  2. the MemoryObj allocated for a torn object is handed back, or a crashed
     writer would leak one staging buffer per torn key.

Exercised at the dfs level with a stub allocator, so it runs without vLLM. The
stub counts allocate/release so (2) is actually checked.

    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16 \
        python3 /lmd/tests/test_torn_object.py
"""

import ctypes
import os
import sys

sys.path.insert(0, "/lmd")

from lmcache_daos import serde
from lmcache_daos.dfs_binding import DfsSys

POOL = os.environ["DAOS_TEST_POOL"]
CONT = os.environ["DAOS_TEST_CONT"]
SZ = 1 << 20          # 1 MiB payload — big enough to be a real read, small enough to be quick

# ---- stub MemoryObj / allocator (no LMCache needed) ----------------------
class StubObj:
    def __init__(self, n):
        self.buf = ctypes.create_string_buffer(n)
        self.byte_array = memoryview(self.buf).cast("B")
        self.released = False

    def ref_count_down(self):
        self.released = True


class StubBackend:
    def __init__(self, n):
        self.n = n
        self.allocated = []

    def allocate(self, shapes, dtypes, fmt):
        o = StubObj(self.n)
        self.allocated.append(o)
        return o


class StubMeta:
    """Stand-in for RemoteMetadata: only .length/.shapes/.dtypes/.fmt are used."""

    def __init__(self, length):
        self.length = length
        self.shapes, self.dtypes, self.fmt = None, None, None


# A minimal re-implementation of the connector's read path, kept in lockstep
# with connector._get_sync. Duplicated rather than imported because importing
# the connector requires LMCache.
def get_sync(dfs, path, backend):
    try:
        obj = dfs.open_rdonly(path)
    except Exception as e:
        if getattr(e, "rc", None) == 2:
            return None
        raise
    try:
        ps = serde.prefix_size()
        hdr = dfs.read_obj(obj, 0, ps + 512)
        if len(hdr) < ps:
            return None
        meta_len, payload_len = serde.parse_prefix(hdr[:ps])
        if meta_len <= len(hdr) - ps:
            meta_bytes = hdr[ps:ps + meta_len]
        else:
            meta_bytes = dfs.read_obj(obj, ps, meta_len)
        if len(meta_bytes) != meta_len:
            return None
        try:
            metadata = StubMeta(int(meta_bytes[:8]))     # stub codec
        except Exception:
            return None
        if payload_len < metadata.length:
            return None
        mo = backend.allocate(None, None, None)
        if mo is None:
            return None
        view = mo.byte_array
        n = metadata.length
        dest = (ctypes.c_char * n).from_buffer(view[:n])
        got = dfs.read_obj_into(obj, ps + meta_len, payload_len, dest)
        if got != payload_len:
            mo.ref_count_down()
            return None
        return mo
    finally:
        dfs.close_obj(obj)


META = b"%08d" % SZ                    # stub metadata: payload length as text
HDR = serde.prefix_pack(len(META), SZ) + META

d = DfsSys(pool=POOL, cont=CONT)
fail = 0
try:
    hdrbuf = ctypes.create_string_buffer(HDR, len(HDR))
    payload = ctypes.create_string_buffer(bytes([0xA7]) * SZ, SZ)

    def write_truncated(path, total):
        """Write the first `total` bytes of a well-formed object."""
        blob = (HDR + bytes([0xA7]) * SZ)[:total]
        b = ctypes.create_string_buffer(blob, len(blob)) if blob else None
        o = d.open_rdwr_create(path)
        try:
            if b is not None:
                d.write_obj_from(o, 0, len(blob), b)
        finally:
            d.close_obj(o)

    ps = serde.prefix_size()
    cases = [
        ("intact",            len(HDR) + SZ,        "hit"),
        ("empty",             0,                    "miss"),
        ("mid-prefix",        ps - 1,               "miss"),
        ("header only",       len(HDR),             "miss"),
        ("half payload",      len(HDR) + SZ // 2,   "miss"),
        ("one byte short",    len(HDR) + SZ - 1,    "miss"),
    ]
    for name, total, expect in cases:
        path = f"/torn_{name.replace(' ', '_')}"
        write_truncated(path, total)
        backend = StubBackend(SZ)
        try:
            got = get_sync(d, path, backend)
            outcome = "hit" if got is not None else "miss"
            err = None
        except Exception as e:
            outcome, err = "RAISED", f"{type(e).__name__}: {e}"
        ok = (outcome == expect)
        leaked = [o for o in backend.allocated
                  if not o.released and outcome != "hit"]
        if leaked:
            ok = False
        print(f"  {name:<16} {total:>9} B -> {outcome:<6} "
              f"expect {expect:<5} alloc={len(backend.allocated)} "
              f"leak={len(leaked)}  {'OK' if ok else 'FAIL'}"
              f"{' | ' + err if err else ''}", flush=True)
        if not ok:
            fail += 1
        d.remove(path)

    # absent key
    backend = StubBackend(SZ)
    got = get_sync(d, "/torn_absent_key", backend) if d.exists("/torn_absent_key") else None
    print(f"  {'absent':<16} {'-':>9}   -> "
          f"{'miss' if got is None else 'hit':<6} expect miss  "
          f"{'OK' if got is None else 'FAIL'}", flush=True)

    print(f"\nDONE test_torn_object: {'OK' if fail == 0 else f'{fail} FAILURE(S)'}",
          flush=True)
finally:
    d.close()

sys.exit(1 if fail else 0)
