#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""The v3 placement keeps v2's torn-object guarantee.

v3 is a placement, not a new header format: the metadata akey holds a v2 header
with the page padding removed. So the thing worth testing is not the codec --
tests/test_serde_v2.py owns that -- but that the reuse is real and that the
akey mapping cannot collide or drift.

The torn shapes are re-run here anyway. In v1/v2 a truncated object is a short
file; in v3 it is a missing or short akey, which is a different failure arriving
at the same code, and "it delegates to v2" is a claim worth checking rather than
asserting.

    python3 tests/test_serde_v3.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmcache_daos import serde_v2 as v2      # noqa: E402
from lmcache_daos import serde_v3 as v3      # noqa: E402

fails = 0


def ck(what, ok, extra=""):
    global fails
    if not ok:
        fails += 1
    print("  %-56s %s %s" % (what, "PASS" if ok else "FAIL", extra))


class FakeKey:
    """Only to_string() is used, which is all dkey_for() reads."""

    def __init__(self, s):
        self._s = s

    def to_string(self):
        return self._s


def roundtrip():
    meta = b"\x01\x02" * 14                      # 28 B, the real metadata size
    buf = v3.pack_meta(meta, payload_len=4 << 20)
    ck("metadata akey is fixed+meta with no padding",
       len(buf) == v3.FIXED_SIZE + len(meta), "(%d B)" % len(buf))
    ck("a v2 header page would have been 4096", v2.HEADER_SIZE == 4096)

    h = v3.parse_meta(buf)
    ck("round trip returns the metadata", h.meta == meta)
    ck("round trip returns the payload length", h.payload_len == 4 << 20)
    ck("round trip is committed", h.committed)

    # The reuse claim, checked rather than asserted: the v3 bytes must be the
    # prefix of the v2 page for the same inputs.
    page = v2.pack_header(meta, 4 << 20)
    ck("v3 bytes are exactly the v2 page minus its padding",
       page[:len(buf)] == buf and set(page[len(buf):]) == {0})
    ck("v2's parser accepts the unpadded form",
       v2.parse_header(buf).meta == meta)


def torn():
    """Every truncation of the metadata akey must read as a miss."""
    meta = b"m" * 40
    full = v3.pack_meta(meta, payload_len=1 << 20)
    cases = [
        ("absent akey (0 bytes)", b""),
        ("mid-fixed-field", full[:20]),
        ("fixed only, metadata missing", full[:v3.FIXED_SIZE]),
        ("metadata truncated", full[:v3.FIXED_SIZE + 10]),
        ("one byte short", full[:-1]),
    ]
    for name, buf in cases:
        try:
            v3.parse_meta(buf)
            ck(name + " -> miss", False, "(accepted!)")
        except v3.BadHeader:
            ck(name + " -> miss", True)

    flipped = bytearray(full)
    flipped[v3.FIXED_SIZE + 5] ^= 0xFF
    try:
        v3.parse_meta(bytes(flipped))
        ck("corrupted metadata byte -> miss", False, "(accepted!)")
    except v3.BadHeader:
        ck("corrupted metadata byte -> miss", True)

    uncommitted = v3.pack_meta(meta, 1 << 20, state=v3.STATE_WRITING)
    try:
        v3.parse_meta(uncommitted)
        ck("uncommitted state -> miss", False, "(accepted!)")
    except v3.BadHeader:
        ck("uncommitted state -> miss", True)
    ck("...but readable when a caller asks for it",
       v3.parse_meta(uncommitted, require_committed=False).meta == meta)

    # The shape v1 cannot express and v3 can: metadata intact, payload short.
    # The header promises a length; the caller compares what it got. Without
    # this check a short fetch would be served as a hit.
    h = v3.parse_meta(full)
    ck("a short payload is detectable from the header",
       not v2.check_payload_len(h, (1 << 20) - 1)
       and v2.check_payload_len(h, 1 << 20))


def keys():
    a, b = FakeKey("model@1@0@aaaa@bf16"), FakeKey("model@1@0@bbbb@bf16")
    da, db = v3.dkey_for(a), v3.dkey_for(b)
    ck("dkey is a raw 32-byte digest, not hex", len(da) == 32, "(%d B)" % len(da))
    ck("different keys give different dkeys", da != db)
    ck("dkey is stable across calls", v3.dkey_for(a) == da)

    # The v1 path hashes the same string, so the two layouts name the same
    # logical object -- which is what makes a migration comparable at all.
    ck("dkey is the raw form of v1's path digest",
       da.hex() == v2.key_to_path(a).rsplit("/", 1)[-1])

    ck("metadata and payload akeys differ", v3.AKEY_META != v3.AKEY_PAYLOAD)
    layers = v3.payload_akeys(40)
    ck("layerwise gives one akey per layer", len(layers) == 40)
    ck("layer akeys are unique", len(set(layers)) == 40)
    ck("layer akeys sort in layer order", list(layers) == sorted(layers))
    ck("no layer akey collides with the metadata akey",
       v3.AKEY_META not in layers)
    ck("whole-chunk mode gives exactly one payload akey",
       v3.payload_akeys(0) == (v3.AKEY_PAYLOAD,))

    for bad in (-1, 1000):
        try:
            v3.akey_layer(bad)
            ck("layer %d rejected" % bad, False, "(accepted!)")
        except ValueError:
            ck("layer %d rejected" % bad, True)


def main():
    print("== round trip ==")
    roundtrip()
    print("== torn metadata reads as a miss ==")
    torn()
    print("== dkey / akey mapping ==")
    keys()
    print("\n  === %s (%d failure%s) ===" % ("ALL PASS" if not fails else "FAILED",
                                             fails, "" if fails == 1 else "s"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
