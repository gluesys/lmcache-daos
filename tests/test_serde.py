"""Pure-Python unit tests for the object length-framing.

Runs anywhere -- no DAOS runtime or LMCache install required.
    python3 tests/test_serde.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmcache_daos import serde  # noqa: E402


def _roundtrip(meta, payload):
    blob = serde.pack(meta, payload)
    got_meta, got_payload = serde.unpack(blob)
    assert got_meta == meta, (got_meta, meta)
    assert got_payload == payload
    # incremental (DFS-style) read path: prefix -> meta -> payload
    meta_len, payload_len = serde.parse_prefix(blob[: serde.prefix_size()])
    assert meta_len == len(meta) and payload_len == len(payload)
    mstart = serde.prefix_size()
    assert blob[mstart:mstart + meta_len] == meta
    pstart = mstart + meta_len
    assert blob[pstart:pstart + payload_len] == payload


def test_basic():
    _roundtrip(b"\x01\x02metadata", b"abcdef")


def test_empty_payload():
    _roundtrip(b"m", b"")


def test_empty_meta():
    _roundtrip(b"", b"payload-only")


def test_large_payload():
    _roundtrip(os.urandom(32), os.urandom(1 << 16))


def test_short_prefix_raises():
    try:
        serde.parse_prefix(b"\x00\x00")
    except ValueError:
        return
    raise AssertionError("expected ValueError on short prefix")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all serde tests passed")
