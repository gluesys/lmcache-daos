#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""The dkey/akey structures match the C ABI, and nothing dangles.

Two failure modes this guards, both silent:

  1. A wrong field offset. ``daos_iod_t`` carries ``iod_flags`` between
     ``iod_size`` and ``iod_nr``; omit it and the binding still compiles, still
     links, and hands DAOS a record count read out of a pointer. The expected
     numbers below came from the C compiler on a host with the headers::

         DaosIod 64  |  iod_name 0  iod_type 24  iod_size 32
                        iod_flags 40  iod_nr 48  iod_recxs 56

     They are written as literals on purpose: a test that recomputes them from
     the same ctypes definition it is checking would pass no matter what.

  2. A buffer freed while DAOS still points at it. ctypes releases an
     unreferenced temporary at once, so a helper that builds an iov from a
     local buffer and returns only the iov produces a dangling pointer that
     usually works -- until it does not. Every builder here returns what must be
     kept alive, and these tests check the pointers still resolve afterwards.

No DAOS required: nothing here opens libdaos.

    python3 tests/test_obj_binding.py
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmcache_daos import obj_binding as ob   # noqa: E402

fails = 0


def ck(what, ok, extra=""):
    global fails
    if not ok:
        fails += 1
    print("  %-54s %s %s" % (what, "PASS" if ok else "FAIL", extra))


# -- 1. the ABI ------------------------------------------------------------
def abi():
    for name, cls, want in (("DaosHandle", ob.DaosHandle, 8),
                            ("DaosObjId", ob.DaosObjId, 16),
                            ("DaosRecx", ob.DaosRecx, 16),
                            ("DaosIod", ob.DaosIod, 64)):
        got = ctypes.sizeof(cls)
        ck("sizeof(%s) == %d" % (name, want), got == want, "(got %d)" % got)

    want = {"iod_name": 0, "iod_type": 24, "iod_size": 32,
            "iod_flags": 40, "iod_nr": 48, "iod_recxs": 56}
    for field, off in want.items():
        got = getattr(ob.DaosIod, field).offset
        ck("daos_iod_t.%s at offset %d" % (field, off), got == off, "(got %d)" % got)

    # The one that a missing iod_flags would move. Called out separately because
    # it is the field whose corruption is hardest to read back from a failure.
    ck("iod_nr is not where iod_flags would put it",
       ob.DaosIod.iod_nr.offset != ob.DaosIod.iod_flags.offset)


# -- 2. builders keep their memory alive -----------------------------------
def builders():
    iov, buf = ob.build_key(b"M")
    ck("build_key sets the length", iov.iov_len == 1 and iov.iov_buf_len == 1,
       "(len=%d cap=%d)" % (iov.iov_len, iov.iov_buf_len))
    ck("build_key's iov points at its buffer",
       iov.iov_buf == ctypes.cast(buf, ctypes.c_void_p).value)
    ck("the byte survives the call",
       ctypes.string_at(iov.iov_buf, 1) == b"M")

    iod, recx, akey_buf = ob.build_iod(b"P", 4096)
    ck("array iod: record size 1, one extent",
       iod.iod_type == ob.DAOS_IOD_ARRAY and iod.iod_size == 1 and iod.iod_nr == 1)
    ck("array iod: extent covers the whole payload",
       bool(iod.iod_recxs) and iod.iod_recxs[0].rx_nr == 4096
       and iod.iod_recxs[0].rx_idx == 0)
    ck("array iod: akey readable after the call",
       ctypes.string_at(iod.iod_name.iov_buf, 1) == b"P")
    ck("array iod: flags zeroed", iod.iod_flags == 0)

    siod, _, _ = ob.build_iod(b"M", 64, single=True)
    ck("single iod: size is the value, no recx",
       siod.iod_type == ob.DAOS_IOD_SINGLE and siod.iod_size == 64
       and not bool(siod.iod_recxs))

    payload = ctypes.create_string_buffer(b"\xa5" * 32, 32)
    sgl, siov = ob.build_sgl(payload, 32)
    ck("sgl has one iov, nothing produced yet",
       sgl.sg_nr == 1 and sgl.sg_nr_out == 0)
    ck("sgl's iov points at the payload",
       ctypes.string_at(sgl.sg_iovs[0].iov_buf, 4) == b"\xa5\xa5\xa5\xa5")


# -- 3. folding: many akeys, one dkey --------------------------------------
def folding():
    bufs = [ctypes.create_string_buffer(bytes([i]) * 16, 16) for i in range(40)]
    items = [(b"L%03d" % i, bufs[i], 16) for i in range(40)]
    iods, sgls, n, keep = ob.build_vectors(items)
    ck("40 layers become 40 iods in one call", n == 40 and len(iods) == 40)

    # The point of the check: every entry must still name ITS OWN akey and point
    # at ITS OWN buffer. A builder that reused one scratch iov would pass a
    # length check and fail here, and the symptom in production would be layers
    # written under the wrong akey -- silent, and exactly the class of bug this
    # project has already paid for once.
    bad = []
    for i in range(40):
        akey = ctypes.string_at(iods[i].iod_name.iov_buf, iods[i].iod_name.iov_len)
        first = ctypes.string_at(sgls[i].sg_iovs[0].iov_buf, 1)
        if akey != b"L%03d" % i or first != bytes([i]):
            bad.append((i, akey, first))
    ck("each iod keeps its own akey and buffer", not bad,
       "" if not bad else "(%d wrong, first=%s)" % (len(bad), bad[0]))
    ck("keepalive holds every referenced object", len(keep) == 40 * 4,
       "(%d)" % len(keep))


def main():
    print("== daos_obj ABI ==")
    abi()
    print("== builders ==")
    builders()
    print("== folding ==")
    folding()
    print("\n  === %s (%d failure%s) ===" % ("ALL PASS" if not fails else "FAILED",
                                             fails, "" if fails == 1 else "s"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
