# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""ctypes binding to the DAOS object API (``daos_obj_*``), the dkey/akey path.

This is the phase-1 deliverable of doc/RAW-API-PLAN.md. It exists beside
``dfs_binding`` rather than replacing it: the container decides which one is
usable, and neither can be pointed at the other's container.

Why bother, in one line each:

  * per-object fixed cost is 0.0137 ms against DFS's 0.63 ms -- 46x
    (doc/LAYERWISE-MEASUREMENT.md), and the fixed part is 90% of a 1 MiB object
  * the layers of one chunk share a dkey, so 40 of them go in one RPC
  * a dead engine can be escaped: blocking loses every thread and an event
    queue loses none (doc/FAILURE-MODES.md)

Structure layouts are taken from the installed headers, not guessed:

    daos_handle_t  { uint64 cookie }                            daos_types.h:78
    daos_obj_id_t  { uint64 lo; uint64 hi }                      daos_types.h:236
    daos_key_t     = d_iov_t                                     daos_types.h:147
    daos_recx_t    { uint64 rx_idx; uint64 rx_nr }               daos_obj.h:346
    daos_iod_t     { daos_key_t iod_name; daos_iod_type_t iod_type;
                     daos_size_t iod_size; uint64 iod_flags;
                     uint32 iod_nr; daos_recx_t *iod_recxs }     daos_obj.h:395

``iod_flags`` sits between ``iod_size`` and ``iod_nr`` and is easy to omit --
doing so shifts ``iod_nr`` and ``iod_recxs`` and produces a binding that
compiles, links, and corrupts. tests/test_obj_binding.py pins the size and every
offset so a header change breaks a test rather than a transfer.

The structure-building helpers are deliberately separate from the I/O so they
can be tested with no DAOS present, which is what phase 1 step 1 asks for.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from typing import List, Optional, Sequence, Tuple

from .dfs_binding import DIov, DSgList, DaosError

_daos = None  # libdaos handle

# daos_obj.h:349 -- DAOS_IOD_NONE / SINGLE / ARRAY
DAOS_IOD_NONE = 0
DAOS_IOD_SINGLE = 1
DAOS_IOD_ARRAY = 2

# daos_obj.h -- open modes
DAOS_OO_RO = 1 << 1
DAOS_OO_RW = 1 << 2

# daos_types.h:81 -- DAOS_HDL_INVAL / DAOS_TX_NONE are both a zeroed handle
DAOS_PC_RO = 1 << 0
DAOS_PC_RW = 1 << 1
DAOS_COO_RO = 1 << 0
DAOS_COO_RW = 1 << 1

# daos_obj_class.h -- OC_UNKNOWN lets daos_obj_generate_oid pick from the pool.
# Not hard-coding S16 on purpose: on a single-rank pool it is rejected outright
# (daos_obj_generate_oid -> DER_INVAL), which cost real time to work out.
OC_UNKNOWN = 0


class DaosHandle(ctypes.Structure):
    """``daos_handle_t`` -- {uint64 cookie}"""

    _fields_ = [("cookie", ctypes.c_uint64)]

    @property
    def valid(self) -> bool:
        return self.cookie != 0


class DaosObjId(ctypes.Structure):
    """``daos_obj_id_t`` -- {uint64 lo; uint64 hi}"""

    _fields_ = [("lo", ctypes.c_uint64), ("hi", ctypes.c_uint64)]


class DaosRecx(ctypes.Structure):
    """``daos_recx_t`` -- {uint64 rx_idx; uint64 rx_nr}"""

    _fields_ = [("rx_idx", ctypes.c_uint64), ("rx_nr", ctypes.c_uint64)]


class DaosIod(ctypes.Structure):
    """``daos_iod_t`` -- one akey's worth of I/O description.

    Field order matters and is copied from daos_obj.h:363-395. See the module
    docstring on iod_flags.
    """

    _fields_ = [
        ("iod_name", DIov),                    # daos_key_t == d_iov_t
        ("iod_type", ctypes.c_int),            # daos_iod_type_t (enum)
        ("iod_size", ctypes.c_uint64),         # daos_size_t
        ("iod_flags", ctypes.c_uint64),
        ("iod_nr", ctypes.c_uint32),
        ("iod_recxs", ctypes.POINTER(DaosRecx)),
    ]


# -- structure building (no DAOS needed; this is what step 1 tests) ---------

def set_iov(iov: DIov, buf, nbytes: int) -> None:
    """Point a d_iov_t at ``nbytes`` of ``buf``.

    ``iov_buf_len`` is the buffer's capacity and ``iov_len`` the valid length.
    On a fetch DAOS writes ``iov_len`` to say how much it produced, which is how
    a short read is detected, so the two are set equal here and read back after.
    """
    iov.iov_buf = ctypes.cast(buf, ctypes.c_void_p)
    iov.iov_buf_len = nbytes
    iov.iov_len = nbytes


def build_key(name: bytes) -> Tuple[DIov, ctypes.Array]:
    """A dkey/akey iov plus the buffer it points into.

    The buffer is returned because the caller must keep it alive: ctypes frees
    an unreferenced temporary immediately, and DAOS would then read a dangling
    pointer. Every dangling-pointer bug in this file's ancestry was this.
    """
    buf = ctypes.create_string_buffer(name, len(name))
    iov = DIov()
    set_iov(iov, buf, len(name))
    return iov, buf


def build_iod(akey: bytes, nbytes: int, single: bool = False):
    """One iod for ``akey`` covering ``nbytes``.

    Returns (iod, recx, akey_buf) -- the caller keeps all three alive.

    ARRAY with a record size of 1 rather than SINGLE: a single value is written
    and read whole, which is right for a few dozen bytes of metadata but not for
    a multi-megabyte payload that a later read may want to slice. Metadata
    passes single=True.
    """
    iov, buf = build_key(akey)
    iod = DaosIod()
    iod.iod_name = iov
    iod.iod_flags = 0
    recx = DaosRecx(0, nbytes)
    if single:
        iod.iod_type = DAOS_IOD_SINGLE
        iod.iod_size = nbytes
        iod.iod_nr = 1
        iod.iod_recxs = None
    else:
        iod.iod_type = DAOS_IOD_ARRAY
        iod.iod_size = 1                       # record size: bytes
        iod.iod_nr = 1
        iod.iod_recxs = ctypes.pointer(recx)
    return iod, recx, buf


def build_sgl(buf, nbytes: int):
    """One sgl pointing at ``buf``. Returns (sgl, iov) -- keep both alive."""
    iov = DIov()
    set_iov(iov, buf, nbytes)
    sgl = DSgList()
    sgl.sg_nr = 1
    sgl.sg_nr_out = 0
    sgl.sg_iovs = ctypes.pointer(iov)
    return sgl, iov


def build_vectors(items: Sequence[Tuple[bytes, object, int]], single: bool = False):
    """Build parallel iod/sgl arrays for several akeys under one dkey.

    ``items`` is a sequence of (akey, buffer, nbytes). This is the call that
    makes folding possible: 40 layers become 40 iods in one RPC instead of 40
    RPCs. Everything the arrays point into is returned in ``keepalive`` and must
    outlive the transfer.
    """
    n = len(items)
    iods = (DaosIod * n)()
    sgls = (DSgList * n)()
    keepalive: List[object] = []
    for i, (akey, buf, nbytes) in enumerate(items):
        iod, recx, akey_buf = build_iod(akey, nbytes, single=single)
        sgl, iov = build_sgl(buf, nbytes)
        iods[i] = iod
        sgls[i] = sgl
        keepalive += [recx, akey_buf, iov, buf]
    return iods, sgls, n, keepalive


# -- library loading -------------------------------------------------------

def _load() -> None:
    """dlopen libdaos and declare prototypes (idempotent).

    Same shape as dfs_binding._load, including DAOS_LIBDIR, so a host carrying
    both a packaged client and a source build resolves the same way for both
    bindings. Mixing the two would be a very confusing failure.
    """
    global _daos
    if _daos is not None:
        return
    libdir = os.environ.get("DAOS_LIBDIR")
    if libdir:
        path = os.path.join(libdir, "libdaos.so")
    else:
        path = ctypes.util.find_library("daos") or "libdaos.so"
    _daos = ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)

    _daos.daos_init.restype = ctypes.c_int
    _daos.daos_init.argtypes = []
    _daos.daos_fini.restype = ctypes.c_int
    _daos.daos_fini.argtypes = []

    _daos.daos_pool_connect.restype = ctypes.c_int
    _daos.daos_pool_connect.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint,
        ctypes.POINTER(DaosHandle), ctypes.c_void_p, ctypes.c_void_p,
    ]
    _daos.daos_pool_disconnect.restype = ctypes.c_int
    _daos.daos_pool_disconnect.argtypes = [DaosHandle, ctypes.c_void_p]

    _daos.daos_cont_open.restype = ctypes.c_int
    _daos.daos_cont_open.argtypes = [
        DaosHandle, ctypes.c_char_p, ctypes.c_uint,
        ctypes.POINTER(DaosHandle), ctypes.c_void_p, ctypes.c_void_p,
    ]
    _daos.daos_cont_close.restype = ctypes.c_int
    _daos.daos_cont_close.argtypes = [DaosHandle, ctypes.c_void_p]

    _daos.daos_obj_generate_oid.restype = ctypes.c_int
    _daos.daos_obj_generate_oid.argtypes = [
        DaosHandle, ctypes.POINTER(DaosObjId), ctypes.c_uint,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
    ]
    _daos.daos_obj_open.restype = ctypes.c_int
    _daos.daos_obj_open.argtypes = [
        DaosHandle, DaosObjId, ctypes.c_uint,
        ctypes.POINTER(DaosHandle), ctypes.c_void_p,
    ]
    _daos.daos_obj_close.restype = ctypes.c_int
    _daos.daos_obj_close.argtypes = [DaosHandle, ctypes.c_void_p]

    _daos.daos_obj_update.restype = ctypes.c_int
    _daos.daos_obj_update.argtypes = [
        DaosHandle, DaosHandle, ctypes.c_uint64, ctypes.POINTER(DIov),
        ctypes.c_uint, ctypes.POINTER(DaosIod), ctypes.POINTER(DSgList),
        ctypes.c_void_p,
    ]
    _daos.daos_obj_fetch.restype = ctypes.c_int
    _daos.daos_obj_fetch.argtypes = [
        DaosHandle, DaosHandle, ctypes.c_uint64, ctypes.POINTER(DIov),
        ctypes.c_uint, ctypes.POINTER(DaosIod), ctypes.POINTER(DSgList),
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    _daos.daos_obj_punch_dkeys.restype = ctypes.c_int
    _daos.daos_obj_punch_dkeys.argtypes = [
        DaosHandle, DaosHandle, ctypes.c_uint64, ctypes.c_uint,
        ctypes.POINTER(DIov), ctypes.c_void_p,
    ]


def loaded() -> bool:
    """True once libdaos is open. Lets a test skip I/O without importing it."""
    return _daos is not None
