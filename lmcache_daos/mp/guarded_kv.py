# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""The MP L2 adapter's storage calls, over the rank guard instead of DFS.

DaosL2Adapter talks to its backend through a handful of DFS-shaped calls
(``stat_size``, ``open_*``, ``write_obj_from``, ``read_obj_into``,
``remove``). This class answers the same calls from an
:class:`~lmcache_daos.rank_guard.S1BucketStore`, so with ``"rank_guard": true``
the adapter gets the deadline, zombie pool and per-rank breaker of the
in-process connector (``DAOS_RANK_GUARD=1``) without its own code changing.

Layout: one key is one dkey (sha256 of the adapter's object name) with two
akeys -- the payload, and its length as 8 bytes written after it. The length
is the commit record, as the metadata akey is in serde_v3: a payload without
it reads as absent, so a put cut off between the two calls is a miss.

Both directions go through a private buffer. In MP mode the MemoryObj is
L1's, and L1 reuses it as soon as the task reports back. A request past its
deadline stays alive in DAOS until the rank returns, and then it still
writes the fetch buffer and still reads the put buffer (check 2a). If those
were L1's buffers, DAOS would overwrite another key's KV in L1, or store
another key's bytes under this one. The private copy belongs to the zombie
instead; the cost is one memcpy per key.

The private buffers come from :class:`_BouncePool`, not ``(c_char * n)()``.
That constructor zero-fills under the GIL: 95 ms for a 203 MiB chunk, so a
40-key load spent 3.8 s allocating one buffer after another whatever the
worker count, and the starved poller turned in-time completions into
deadline misses (cxl2 4 ranks, 2026-11-14: 2.6 GB/s and 37/40 hits at 16
workers, against 10.8 GB/s for DFS). A buffer goes back to the pool only
after its op completed; one a timed-out op may still be using is left to
the zombie that holds it.
"""
from __future__ import annotations

import ctypes
import mmap
import threading
from typing import Dict, List, Optional

from .. import serde_v3
from ..rank_guard import Dropped, GuardedObj, S1BucketStore

AKEY_DATA = b"d"
AKEY_LEN = b"n"


class _BouncePool:
    """Reusable private buffers, keyed by length.

    Buffers are anonymous mmaps: allocating one does not touch its pages and
    holds no GIL, and a reused one is already faulted in. At most
    ``max_bytes`` of idle buffers are kept; past that a returned buffer is
    simply dropped (unmapped when its last reference goes).
    """

    def __init__(self, max_bytes: int = 4 << 30):
        self.max_bytes = max_bytes
        self._idle: Dict[int, List[ctypes.Array]] = {}
        self._idle_bytes = 0
        self._lock = threading.Lock()

    def get(self, n: int) -> ctypes.Array:
        with self._lock:
            free = self._idle.get(n)
            if free:
                self._idle_bytes -= n
                return free.pop()
        # The array keeps the mmap alive through its _objects reference.
        return (ctypes.c_char * n).from_buffer(mmap.mmap(-1, n))

    def put(self, buf: ctypes.Array) -> None:
        n = len(buf)
        with self._lock:
            if self._idle_bytes + n > self.max_bytes:
                return
            self._idle.setdefault(n, []).append(buf)
            self._idle_bytes += n

    def idle_bytes(self) -> int:
        with self._lock:
            return self._idle_bytes


class GuardedKV:
    def __init__(self, store: S1BucketStore, bounce_max_bytes: int = 4 << 30):
        self._obj = GuardedObj(store)
        self.store = store
        self._bounce = _BouncePool(bounce_max_bytes)

    @staticmethod
    def _dkey(path: str) -> bytes:
        return serde_v3.dkey_for_str(path)

    # -- the DFS-shaped surface DaosL2Adapter uses ---------------------------
    def mkdir_p(self, path: str) -> None:
        return None                      # no directories in a dkey namespace

    def stat_size(self, path: str) -> Optional[int]:
        """Committed length, or None: absent, uncommitted, or not reachable
        now (a skipped or timed-out rank is a miss, not an error)."""
        buf = ctypes.create_string_buffer(8)
        (got,) = self._obj.fetch(self._dkey(path), [(AKEY_LEN, buf, 8)], single=True)
        if got != 8:
            return None
        return int.from_bytes(buf.raw, "little")

    def open_rdwr_create(self, path: str, oclass: int = 0) -> str:
        return path

    def open_rdonly(self, path: str) -> str:
        return path                      # absence shows up as a short read

    def close_obj(self, h) -> None:
        return None

    def write_obj_from(self, h: str, offset: int, length: int, src) -> int:
        """Store ``length`` bytes. Returns ``length``, or 0 when the put was
        dropped (the adapter then counts the key as failed)."""
        if offset != 0:
            raise ValueError("GuardedKV stores whole objects only")
        dkey = self._dkey(h)
        copy = self._bounce.get(length)
        ctypes.memmove(copy, src, length)
        size = (ctypes.c_char * 8).from_buffer_copy(length.to_bytes(8, "little"))
        try:
            self._obj.update(dkey, [(AKEY_DATA, copy, length)])
        except Dropped:
            return 0                     # copy may still be read by a zombie
        self._bounce.put(copy)
        try:
            self._obj.update(dkey, [(AKEY_LEN, size, 8)], single=True)
        except Dropped:
            return 0
        return length

    def read_obj_into(self, h: str, offset: int, length: int, dest) -> int:
        """Read the committed object into ``dest``. Returns ``length`` or 0."""
        if offset != 0:
            raise ValueError("GuardedKV loads whole objects only")
        have = self.stat_size(h)
        if have != length:
            return 0                     # absent, uncommitted, or another size
        # A fetch past its deadline keeps this buffer in its zombie's hold and
        # reports 0 bytes; dest (L1's) is never handed to DAOS.
        bounce = self._bounce.get(length)
        (got,) = self._obj.fetch(self._dkey(h), [(AKEY_DATA, bounce, length)])
        if got != length:
            return 0                     # bounce may still be written by a zombie
        ctypes.memmove(dest, bounce, length)
        self._bounce.put(bounce)
        return length

    def remove(self, path: str) -> bool:
        try:
            self._obj.punch(self._dkey(path))
        except Dropped:
            return False
        return True

    def close(self) -> None:
        self._obj.close()


__all__ = ["GuardedKV", "AKEY_DATA", "AKEY_LEN"]
