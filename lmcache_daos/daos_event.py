"""DAOS event / event-queue bindings — P0 of the async refactor.

Why this module exists
----------------------
``dfs_sys_read()`` already takes a ``daos_event_t *``; passing NULL is what makes
our current reads synchronous. Driving it asynchronously needs an event queue and
an event object, which means declaring ``daos_event_t`` — a struct whose payload
is explicitly marked *internal use* in the DAOS headers::

    typedef struct daos_event {
        int      ev_error;
        /* Internal use - 152 + 8 bytes pad for pthread_mutex_t size
           difference on __aarch64__ */
        struct { uint64_t space[20]; } ev_private;
        uint64_t ev_debug;
    } daos_event_t;

Hard-coding that layout is the risky part of the whole refactor. If a future DAOS
grows ``space[20]`` to ``space[24]``, DAOS writes past the end of the buffer we
allocated: **silent heap corruption** that surfaces much later somewhere
unrelated. That is the worst available failure mode, so this module refuses to
guess quietly. Two tiers, per the refactoring plan:

1. **Preferred — ask the C side.** If ``libdaos_evshim.so`` is loadable (see
   ``shim/daos_evshim.c``, ~30 lines) we call ``daos_ev_size()`` and allocate
   exactly what the *compiled* header says. No layout assumption at all.
2. **Fallback — assume, then prove.** Use the ctypes layout above, but verify it
   at startup with a canary: allocate twice the assumed size, poison the back
   half, run a real init → async read → poll → fini cycle, and confirm the poison
   is untouched. A layout mismatch becomes an immediate, loud failure instead of
   corruption.

``verify_abi()`` raises :class:`DaosEventABIError` on mismatch. Callers are
expected to treat that as fatal — that is the entire point.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from typing import List, Optional

from .dfs_binding import DIov, DSgList, DaosError, libdaos

# ---- daos_event.h constants ----------------------------------------------
DAOS_EQ_WAIT = -1        # block until at least one event completes
DAOS_EQ_NOWAIT = 0       # return immediately
DAOS_EQ_DESTROY_FORCE = 1

# Guard region appended after every event allocation. 256 B rather than a
# token few: the guard is only useful if it is wider than any plausible growth
# of ev_private, otherwise a struct that grew a lot would write straight past
# the canary and corrupt the heap anyway -- the exact failure we are guarding
# against. 256 B also keeps the total allocation comfortably larger than
# sizeof(DaosEvent), which is what lets the negative-control test understate
# EVENT_SIZE and still get a real (safe) canary hit instead of a ctypes
# "buffer too small" rejection.
_CANARY = b"\xA5\x5A\xC3\x3C" * 64   # 256 B


class DaosEventABIError(RuntimeError):
    """The in-memory ``daos_event_t`` layout does not match our declaration.

    Treat as fatal. Continuing would let DAOS write outside the allocation.
    """


class DaosHandle(ctypes.Structure):
    """``daos_handle_t`` — a single ``uint64_t`` cookie, passed by value.

    Declared as a Structure (not a bare c_uint64) so ctypes applies the correct
    small-struct-by-value convention on every ABI, not just SysV AMD64.
    """

    _fields_ = [("cookie", ctypes.c_uint64)]

    def __bool__(self) -> bool:
        return self.cookie != 0


class _EvPrivate(ctypes.Structure):
    _fields_ = [("space", ctypes.c_uint64 * 20)]


class DaosEvent(ctypes.Structure):
    """``daos_event_t`` as declared in DAOS 2.8 ``daos_event.h``."""

    _fields_ = [
        ("ev_error", ctypes.c_int),
        ("ev_private", _EvPrivate),
        ("ev_debug", ctypes.c_uint64),
    ]


#: Size we will allocate per event. Overwritten by the shim's authoritative
#: value if the shim is available (see ``_probe_event_size``).
EVENT_SIZE = ctypes.sizeof(DaosEvent)

#: True when ``EVENT_SIZE`` came from the C side rather than our declaration.
EVENT_SIZE_FROM_SHIM = False

_shim = None
_protos_done = False


# ---- library wiring -------------------------------------------------------
def _probe_event_size() -> None:
    """Prefer the shim's ``daos_ev_size()`` over our compiled-in assumption."""
    global _shim, EVENT_SIZE, EVENT_SIZE_FROM_SHIM
    if _shim is not None or EVENT_SIZE_FROM_SHIM:
        return
    path = os.environ.get("DAOS_EVSHIM_PATH") or ctypes.util.find_library(
        "daos_evshim") or "libdaos_evshim.so"
    try:
        lib = ctypes.CDLL(path)
        lib.daos_ev_size.restype = ctypes.c_size_t
        lib.daos_ev_size.argtypes = []
        size = int(lib.daos_ev_size())
    except Exception:
        return  # shim absent -> fall back to the canary-verified assumption
    if size <= 0 or size > 4096:
        return  # implausible; ignore rather than trust it
    _shim = lib
    EVENT_SIZE = size
    EVENT_SIZE_FROM_SHIM = True


def _declare() -> ctypes.CDLL:
    """Declare the event/EQ prototypes on libdaos (idempotent)."""
    global _protos_done
    lib = libdaos()
    if _protos_done:
        return lib
    _probe_event_size()

    ev_p = ctypes.POINTER(DaosEvent)

    # int daos_eq_create(daos_handle_t *eqh);
    lib.daos_eq_create.restype = ctypes.c_int
    lib.daos_eq_create.argtypes = [ctypes.POINTER(DaosHandle)]

    # int daos_eq_destroy(daos_handle_t eqh, int flags);
    lib.daos_eq_destroy.restype = ctypes.c_int
    lib.daos_eq_destroy.argtypes = [DaosHandle, ctypes.c_int]

    # int daos_event_init(daos_event_t *ev, daos_handle_t eqh,
    #                     daos_event_t *parent);
    lib.daos_event_init.restype = ctypes.c_int
    lib.daos_event_init.argtypes = [ev_p, DaosHandle, ev_p]

    # int daos_event_fini(daos_event_t *ev);
    lib.daos_event_fini.restype = ctypes.c_int
    lib.daos_event_fini.argtypes = [ev_p]

    # int daos_event_abort(daos_event_t *ev);
    lib.daos_event_abort.restype = ctypes.c_int
    lib.daos_event_abort.argtypes = [ev_p]

    # int daos_eq_poll(daos_handle_t eqh, int wait_running, int64_t timeout,
    #                  unsigned int nevents, daos_event_t **events);
    # Returns the number of completed events, or a negative DER_ code.
    lib.daos_eq_poll.restype = ctypes.c_int
    lib.daos_eq_poll.argtypes = [
        DaosHandle, ctypes.c_int, ctypes.c_int64, ctypes.c_uint,
        ctypes.POINTER(ev_p),
    ]

    _protos_done = True
    return lib


# ---- event allocation -----------------------------------------------------
class EventSlot:
    """One ``daos_event_t`` plus the tail canary that guards its allocation.

    The event is never a bare ``DaosEvent()``: we always allocate
    ``EVENT_SIZE + len(_CANARY)`` and keep a poisoned tail, so an ABI drift
    shows up as a canary check failure rather than as heap corruption. The cost
    is 256 bytes per in-flight read, which is noise next to a multi-MB chunk.
    """

    __slots__ = ("_buf", "ev", "_canary_off")

    def __init__(self, canary: bool = True):
        tail = len(_CANARY) if canary else 0
        self._buf = ctypes.create_string_buffer(EVENT_SIZE + tail)
        self._canary_off = EVENT_SIZE if canary else -1
        if canary:
            self._buf[EVENT_SIZE:EVENT_SIZE + tail] = _CANARY
        # from_buffer aliases the allocation; no copy, and the buffer keeps the
        # memory alive as long as this slot is referenced.
        self.ev = DaosEvent.from_buffer(self._buf)

    @property
    def ptr(self):
        return ctypes.byref(self.ev)

    @property
    def addr(self) -> int:
        """Address of the event — the pending-table key ``daos_eq_poll`` hands back."""
        return ctypes.addressof(self.ev)

    def canary_ok(self) -> bool:
        if self._canary_off < 0:
            return True
        end = self._canary_off + len(_CANARY)
        return self._buf[self._canary_off:end] == _CANARY

    def repoison(self) -> None:
        """Re-write the guard pattern (used when recycling a slot)."""
        if self._canary_off < 0:
            return
        end = self._canary_off + len(_CANARY)
        self._buf[self._canary_off:end] = _CANARY

    def error(self) -> int:
        return self.ev.ev_error


class EventQueue:
    """A DAOS event queue with a single owner thread.

    ``daos_eq_poll``'s concurrency guarantees are not stated in the headers, so
    we deliberately do not share one EQ across pollers: one EQ per connector,
    one polling thread. That also removes the reason the connector needed
    per-thread ``dfs_sys`` handles (the ``DFS_SYS_NO_LOCK`` workaround), which is
    the structural win of this refactor.
    """

    def __init__(self):
        lib = _declare()
        self._lib = lib
        self.eqh = DaosHandle()
        rc = lib.daos_eq_create(ctypes.byref(self.eqh))
        if rc != 0:
            raise DaosError("daos_eq_create", rc)

    # -- event lifecycle ---------------------------------------------------
    def new_event(self, canary: bool = True) -> EventSlot:
        """Allocate and ``daos_event_init`` a slot bound to this queue."""
        slot = EventSlot(canary=canary)
        rc = self._lib.daos_event_init(slot.ptr, self.eqh, None)
        if rc != 0:
            raise DaosError("daos_event_init", rc)
        return slot

    def fini_event(self, slot: EventSlot) -> None:
        """``daos_event_fini`` — must run before the slot's buffer is dropped."""
        rc = self._lib.daos_event_fini(slot.ptr)
        if rc != 0:
            raise DaosError("daos_event_fini", rc)

    def reinit_event(self, slot: EventSlot) -> None:
        """Recycle a completed slot: ``daos_event_fini`` then ``_init`` again.

        Reusing the allocation avoids a fresh 176 B + 256 B guard per read on a
        hot path. Only valid on a slot whose completion has already been
        harvested -- re-initialising an in-flight event is undefined.
        """
        rc = self._lib.daos_event_fini(slot.ptr)
        if rc != 0:
            raise DaosError("daos_event_fini(recycle)", rc)
        # Re-poison the guard: fini/init may legitimately touch the struct, and
        # a stale canary would silently stop guarding.
        slot.repoison()
        rc = self._lib.daos_event_init(slot.ptr, self.eqh, None)
        if rc != 0:
            raise DaosError("daos_event_init(recycle)", rc)

    def abort_event(self, slot: EventSlot) -> None:
        self._lib.daos_event_abort(slot.ptr)

    # -- completion harvesting --------------------------------------------
    def poll(self, max_events: int = 16, wait: bool = True,
             timeout_us: int = 1000) -> List[int]:
        """Harvest completions; return their event **addresses**.

        Addresses (not objects) are returned because that is the only identity
        DAOS gives back — the caller maps them through its pending table to
        recover the buffer/size/handle it must keep alive. ``timeout_us`` is
        passed straight through; ``wait=False`` uses ``DAOS_EQ_NOWAIT``.
        """
        n = max(1, int(max_events))
        arr = (ctypes.POINTER(DaosEvent) * n)()
        rc = self._lib.daos_eq_poll(
            self.eqh, 1 if wait else 0,
            DAOS_EQ_NOWAIT if not wait else int(timeout_us),
            n, arr)
        if rc < 0:
            raise DaosError("daos_eq_poll", rc)
        out = []
        for i in range(rc):
            p = arr[i]
            if p:
                out.append(ctypes.addressof(p.contents))
        return out

    def close(self, force: bool = True) -> None:
        if self.eqh.cookie:
            self._lib.daos_eq_destroy(
                self.eqh, DAOS_EQ_DESTROY_FORCE if force else 0)
            self.eqh = DaosHandle()


# ---- pending-table entry --------------------------------------------------
class AsyncRead:
    """One in-flight ``dfs_read`` and every object DAOS touches until it lands.

    This class *is* the lifetime contract. DAOS keeps raw pointers to all of the
    following for the duration of the operation, so a single strong reference to
    an ``AsyncRead`` in the connector's pending table keeps the whole set alive:

    ==========  ====================================================
    ``slot``    the ``daos_event_t`` (plus its guard canary)
    ``dest``    destination buffer -- typically aliasing a MemoryObj
    ``_iov``    ``d_iov_t`` pointing at ``dest``
    ``_sgl``    ``d_sg_list_t`` pointing at ``_iov``
    ``_size``   the ``daos_size_t *`` in/out parameter
    ``obj``     the ``dfs_obj_t`` handle -- must NOT be closed on submit
    ==========  ====================================================

    ``_iov``/``_sgl`` are the two the original plan missed: they only became a
    caller responsibility once we moved off ``dfs_sys_read`` (which hid them on
    its stack, and is precisely why it cannot be used asynchronously).
    """

    __slots__ = ("key", "slot", "obj", "dest", "length", "offset",
                 "_size", "_iov", "_sgl")

    def __init__(self, key, slot: EventSlot, obj, dest, length: int,
                 offset: int = 0):
        self.key = key
        self.slot = slot
        self.obj = obj
        self.dest = dest
        self.length = length
        self.offset = offset
        self._size = ctypes.c_ulonglong(length)
        buf_addr = ctypes.cast(dest, ctypes.c_void_p)
        self._iov = DIov(buf_addr, length, length)
        self._sgl = DSgList(1, 1, ctypes.pointer(self._iov))

    @property
    def addr(self) -> int:
        """Pending-table key: the event address ``daos_eq_poll`` returns."""
        return self.slot.addr

    def submit(self, dfs) -> None:
        dfs.submit_read(self.obj, self.offset, self._sgl, self._size,
                        self.slot.addr)

    def bytes_read(self) -> int:
        return self._size.value

    def check(self) -> int:
        """Validate a harvested completion; return bytes read.

        Checks all three failure channels: the guard canary (ABI drift), the
        event's ``ev_error`` (the operation failed after submission), and a short
        read (fewer bytes than requested).
        """
        if not self.slot.canary_ok():
            raise DaosEventABIError(
                f"event canary clobbered during async read: "
                f"sizeof(daos_event_t) > {EVENT_SIZE} B on this build")
        err = self.slot.error()
        if err != 0:
            raise DaosError("dfs_read(async)", err)
        got = self._size.value
        if got != self.length:
            raise DaosError(
                f"dfs_read(async) short read {got}/{self.length}", 5)  # EIO
        return got


# ---- P0 gate: ABI canary verification ------------------------------------
def verify_abi(dfs, path: str, length: int = 4096) -> dict:
    """Prove the ``daos_event_t`` layout by exercising a real async read.

    ``daos_event_init`` alone barely touches the struct, so a size mismatch can
    hide until DAOS actually drives the event. This runs the full cycle —
    init → async ``dfs_sys_read`` → ``daos_eq_poll`` → ``fini`` — against a real
    object at ``path``, then checks the tail canary.

    Returns a dict of what was verified. Raises :class:`DaosEventABIError` if the
    canary was touched (fatal: our allocation is too small for this DAOS build).
    """
    eq = EventQueue()
    slot = None
    try:
        slot = eq.new_event(canary=True)
        if not slot.canary_ok():
            raise DaosEventABIError(
                f"canary already clobbered by daos_event_init: our "
                f"sizeof(daos_event_t)={EVENT_SIZE} is too small "
                f"(shim={EVENT_SIZE_FROM_SHIM})")

        obj = dfs.open_rdonly(path)
        try:
            dest = ctypes.create_string_buffer(length)
            # AsyncRead owns the sgl/iov/size/dest set for the whole operation;
            # `op` stays referenced by this frame until the event is harvested,
            # which is the same contract the connector's pending table encodes.
            op = AsyncRead("abi-probe", slot, obj, dest, length)
            op.submit(dfs)
            done = []
            for _ in range(2000):          # ~2 s worst case
                done = eq.poll(max_events=4, wait=True, timeout_us=1000)
                if done:
                    break
            if not done:
                raise DaosEventABIError(
                    "async read never completed; cannot validate the event ABI")
            if slot.addr not in done:
                raise DaosEventABIError(
                    f"daos_eq_poll returned an unexpected event address "
                    f"{done!r}, expected {slot.addr:#x} -- event identity is "
                    f"not usable as a pending-table key on this build")
            if not slot.canary_ok():
                raise DaosEventABIError(
                    f"canary clobbered after async read: sizeof(daos_event_t) "
                    f"is larger than our {EVENT_SIZE} B allocation "
                    f"(shim={EVENT_SIZE_FROM_SHIM}). Refusing to continue -- "
                    f"this would be silent heap corruption. Build "
                    f"shim/daos_evshim.c and set DAOS_EVSHIM_PATH.")
            got = op.check()
            return {
                "event_size": EVENT_SIZE,
                "size_from_shim": EVENT_SIZE_FROM_SHIM,
                "bytes_read": got,
                "canary_ok": True,
                "poll_identity_ok": True,
            }
        finally:
            dfs.close_obj(obj)
    finally:
        if slot is not None:
            try:
                eq.fini_event(slot)
            except Exception:
                pass
        eq.close()


def verify_abi_or_die(dfs, path: str, length: int = 4096) -> dict:
    """``verify_abi`` with the failure mode the plan calls for: loud and fatal."""
    try:
        info = verify_abi(dfs, path, length)
    except DaosEventABIError as e:
        raise SystemExit(
            f"FATAL: DAOS event ABI check failed -- {e}\n"
            f"Async DAOS I/O is disabled to avoid silent memory corruption.")
    return info
