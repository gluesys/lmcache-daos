# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Unit tests for the v2 page-aligned object format (no LMCache/DAOS needed).

    python3 tests/test_serde_v2.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lmcache_daos import serde, serde_v2 as v2  # noqa: E402


def test_roundtrip():
    meta = b"\x01\x02meta-bytes" * 3
    page = v2.pack_header(meta, 40 << 20)
    assert len(page) == v2.HEADER_SIZE
    h = v2.parse_header(page)
    assert h.committed and h.meta == meta and h.payload_len == 40 << 20
    assert h.total_len == v2.HEADER_SIZE + (40 << 20)
    assert v2.payload_offset() == 4096
    assert v2.check_payload_len(h, 40 << 20) and not v2.check_payload_len(h, (40 << 20) - 1)


def test_empty_meta_and_zero_payload():
    h = v2.parse_header(v2.pack_header(b"", 0))
    assert h.meta == b"" and h.payload_len == 0


def test_uncommitted_is_a_miss():
    page = v2.pack_header(b"m", 10, state=v2.STATE_WRITING)
    try:
        v2.parse_header(page)
        assert False, "uncommitted header accepted"
    except v2.BadHeader as e:
        assert "not committed" in str(e)
    h = v2.parse_header(page, require_committed=False)
    assert not h.committed and h.payload_len == 10


def test_corruption_detected():
    page = bytearray(v2.pack_header(b"meta", 123))
    for off in (0, 8, 12, 16, 24, v2.FIXED_SIZE):   # magic, version, state, meta_len, payload_len, meta
        bad = bytearray(page)
        bad[off] ^= 0xFF
        try:
            v2.parse_header(bytes(bad))
            assert False, f"corruption at {off} not detected"
        except v2.BadHeader:
            pass
    # flipping the state bit alone (WRITING<->COMMITTED) must fail the CRC too
    bad = bytearray(page)
    bad[12] ^= 0x01
    try:
        v2.parse_header(bytes(bad), require_committed=False)
        assert False
    except v2.BadHeader as e:
        assert "CRC" in str(e)


def test_short_reads():
    page = v2.pack_header(b"x" * 100, 5)
    for n in (0, 8, v2.FIXED_SIZE - 1, v2.FIXED_SIZE + 50):
        try:
            v2.parse_header(page[:n])
            assert False, f"short header of {n} accepted"
        except v2.BadHeader:
            pass
    # a header page shorter than HEADER_SIZE but containing the whole meta is fine
    assert v2.parse_header(page[:v2.FIXED_SIZE + 100]).meta == b"x" * 100


def test_meta_limits():
    v2.pack_header(b"m" * v2.MAX_META, 1)
    try:
        v2.pack_header(b"m" * (v2.MAX_META + 1), 1)
        assert False
    except ValueError:
        pass


def test_paths():
    class K:
        def to_string(self):
            return "Qwen/Qwen3-14B@0@abc/with/slashes"
    p = v2.key_to_path(K())
    assert p.startswith("/v2/") and len(p) == len("/v2/") + 64 and "/" not in p[4:]
    t = v2.temp_path(p, "1a2b")
    assert t.startswith("/v2/.tmp-") and t.endswith("-1a2b") and t.count("/") == 2
    # v1 and v2 never collide
    assert p != "/" + p[4:]


def test_v1_migration_helper():
    blob = serde.pack(b"M", b"PAYLOAD")
    assert v2.split_v1_blob(blob) == (b"M", b"PAYLOAD")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e))
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
