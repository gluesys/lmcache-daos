#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""DaosConnector with DAOS_RANK_GUARD=1 against a real node outage.

rank_guard_outage.py drives S1BucketStore directly. This drives the
connector's own get/put code (_get_raw, _put_raw, _fetch_into) over it, on a
client without LMCache: RemoteMetadata, the CPU allocator and MemoryObj are
small fakes, the rest is the real module. Same output shape as
rank_guard_outage.py, one line per second, plus the allocator's view:

  * ``parked``  -- MemoryObjs released by the poller thread, i.e. a payload
    fetch that hit its deadline and was given back only at the reap;
  * ``out``     -- MemoryObjs allocated and not yet released. It must return
    to 0 after the rank is back: anything left is a leak.

    DAOS_RANK_GUARD=1 python3 tests/rank_guard_connector_outage.py POOL CONT \\
        --keys 600 --secs 900

A pool for the ``kvcache`` profile is assumed: rd_fac 0, ``self_heal:none``,
non-POSIX container. Power a storage node off and on while it runs.
"""
import argparse
import asyncio
import collections
import json
import logging
import os
import random
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmcache_daos import connector as C       # noqa: E402

VSIZE = 65536


def value(i: int) -> bytes:
    return i.to_bytes(8, "little") * (VSIZE // 8)


class FakeMeta:
    """RemoteMetadata's surface as the connector uses it."""

    def __init__(self, length, shapes, dtypes, fmt):
        self.length, self.shapes, self.dtypes, self.fmt = length, shapes, dtypes, fmt

    def serialize(self) -> bytes:
        return struct.pack("<QI", self.length, self.fmt)

    @classmethod
    def deserialize(cls, b: bytes) -> "FakeMeta":
        length, fmt = struct.unpack("<QI", bytes(b[:12]))
        return cls(length, [(length,)], ["u8"], fmt)


class FakeMO:
    def __init__(self, alloc, idx):
        self.alloc, self.idx = alloc, idx
        self.buf = bytearray(VSIZE)
        self.byte_array = memoryview(self.buf)

    def get_shapes(self):
        return [(VSIZE,)]

    def get_dtypes(self):
        return ["u8"]

    def get_memory_format(self):
        return 0

    def ref_count_down(self):
        self.alloc.release(self)


class FakeAlloc:
    """A fixed pool handed out LIFO, so a buffer released too early is the
    first one reused -- the case _fetch_into exists to prevent."""

    def __init__(self, n):
        self.lock = threading.Lock()
        self.free = [FakeMO(self, i) for i in range(n)]
        self.busy = set()
        self.parked = 0
        self.double = 0
        self.exhausted = 0

    def allocate(self, shapes, dtypes, fmt):
        with self.lock:
            if not self.free:
                self.exhausted += 1
                return None
            mo = self.free.pop()
            self.busy.add(mo.idx)
            return mo

    def release(self, mo):
        with self.lock:
            if mo.idx not in self.busy:
                self.double += 1
                return
            self.busy.discard(mo.idx)
            self.free.append(mo)
            if threading.current_thread().name == "daos-eq-poller":
                self.parked += 1


class Key:
    def __init__(self, i):
        self.s = "rgc@1@0@%016x@u8" % i

    def to_string(self):
        return self.s


class LCB:
    def __init__(self, url, alloc):
        self.config = type("Cfg", (), {"remote_url": url})()
        self.metadata = None
        self.allocate = alloc.allocate


async def run(a) -> int:
    loop = asyncio.get_running_loop()
    alloc = FakeAlloc(a.pool_objs)
    c = C.DaosConnector(url="daos://%s/%s" % (a.pool, a.cont), loop=loop,
                        local_cpu_backend=LCB(None, alloc))
    st = c._obj.store
    t0 = time.monotonic()
    if not await loop.run_in_executor(None, st.ready.wait, a.connect_wait):
        print(json.dumps({"event": "not_ready", "after": a.connect_wait}), flush=True)
        return 2
    if st.error:
        raise st.error
    print(json.dumps({"event": "ready", "secs": round(time.monotonic() - t0, 2)}),
          flush=True)

    keys = [Key(i) for i in range(a.keys)]

    async def put(i):
        mo = alloc.allocate(None, None, None)
        mo.buf[:] = value(i)
        try:
            await c.put(keys[i], mo)
        finally:
            mo.ref_count_down()           # InstrumentedRemoteConnector's drop

    for i in range(a.keys):
        await put(i)
    rank_of = {i: st.placement.rank(st.placement.bucket(C._raw_addr(k)[0]))
               for i, k in enumerate(keys)}
    print(json.dumps({"event": "seeded", "per_rank":
                      collections.Counter(rank_of.values())}), flush=True)

    rnd = random.Random(1)
    end = time.monotonic() + a.secs
    bad = 0
    while time.monotonic() < end:
        sec_end = time.monotonic() + 1.0
        out = collections.defaultdict(collections.Counter)
        worst = collections.defaultdict(float)
        lat = collections.defaultdict(list)
        while time.monotonic() < sec_end:
            i = rnd.randrange(a.keys)
            r = rank_of[i]
            s = time.monotonic()
            if rnd.random() < a.put_ratio:
                try:
                    await put(i)
                    out[r]["put_ok"] += 1
                except Exception as e:
                    out[r]["put_" + type(e).__name__] += 1
            else:
                try:
                    mo = await c.get(keys[i])
                except Exception as e:
                    out[r]["get_" + type(e).__name__] += 1
                    mo = None
                else:
                    if mo is None:
                        out[r]["miss"] += 1
                    elif bytes(mo.buf) != value(i):
                        bad += 1
                        out[r]["BADDATA"] += 1
                    else:
                        out[r]["hit"] += 1
                if mo is not None:
                    mo.ref_count_down()
            dt = time.monotonic() - s
            worst[r] = max(worst[r], dt)
            lat[r].append(dt)
        snap = st.snapshot()
        print(json.dumps({"t": round(time.time(), 1),
                          "ranks": {str(r): {"state": v["state"], "zombies": v["zombies"]}
                                    for r, v in snap["ranks"].items()},
                          "ops": {str(r): dict(x) for r, x in sorted(out.items())},
                          "worst": {str(r): round(v, 3) for r, v in sorted(worst.items())},
                          "p50": {str(r): round(sorted(v)[len(v) // 2], 4)
                                  for r, v in sorted(lat.items())},
                          "zombies": snap["zombies"],
                          "alloc": {"out": len(alloc.busy), "parked": alloc.parked,
                                    "double": alloc.double,
                                    "exhausted": alloc.exhausted}}), flush=True)
    print(json.dumps({"event": "end", "stats": st.snapshot()["stats"], "bad": bad,
                      "alloc": {"out": len(alloc.busy), "parked": alloc.parked,
                                "double": alloc.double}}), flush=True)
    return 1 if bad or alloc.double else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pool")
    ap.add_argument("cont")
    ap.add_argument("--keys", type=int, default=600)
    ap.add_argument("--secs", type=float, default=900)
    ap.add_argument("--put-ratio", type=float, default=0.1)
    ap.add_argument("--pool-objs", type=int, default=256)
    ap.add_argument("--connect-wait", type=float, default=60)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if os.environ.get("DAOS_RANK_GUARD") != "1":
        print("set DAOS_RANK_GUARD=1", file=sys.stderr)
        return 2
    C._HAS_LMCACHE = True                 # the fakes above stand in for it
    C.RemoteMetadata = FakeMeta
    rc = asyncio.run(run(a))
    sys.stdout.flush()
    os._exit(rc)                          # zombies may remain; do not run daos_fini


if __name__ == "__main__":
    sys.exit(main())
