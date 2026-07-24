"""Length-framing for the DAOS LMCache backend's on-disk objects.

Each stored object is one DFS file laid out as::

    +-----------------------------------------------+
    | prefix  : '<II' = meta_len(4) payload_len(4)  |  8 bytes
    +-----------------------------------------------+
    | meta    : opaque metadata bytes               |  meta_len bytes
    +-----------------------------------------------+
    | payload : raw KV-cache bytes                  |  payload_len bytes
    +-----------------------------------------------+

The ``meta`` region is treated as **opaque** here -- the connector fills it with
LMCache's own ``RemoteMetadata.serialize()`` output so multi-tensor
shapes/dtypes/format are encoded exactly the way LMCache expects. Keeping the
metadata *inside* the file makes each object self-describing: read the 8-byte
prefix, then meta, then payload -- no separate stat/index lookup.

This module has **no dependency on LMCache or DAOS** so it stays unit-testable
anywhere (see tests/test_serde.py).
"""

from __future__ import annotations

import struct
from typing import Tuple

_PREFIX = struct.Struct("<II")  # meta_len, payload_len


def prefix_size() -> int:
    return _PREFIX.size


def parse_prefix(prefix: bytes) -> Tuple[int, int]:
    """Validate the fixed prefix; return (meta_len, payload_len)."""
    if len(prefix) != _PREFIX.size:
        raise ValueError(f"short prefix: {len(prefix)} != {_PREFIX.size}")
    return _PREFIX.unpack(prefix)


def pack(meta: bytes, payload: bytes) -> bytes:
    """Full object bytes for callers that write in one shot."""
    return _PREFIX.pack(len(meta), len(payload)) + meta + payload


def unpack(blob: bytes) -> Tuple[bytes, bytes]:
    """Split a full object back into (meta_bytes, payload_bytes)."""
    meta_len, payload_len = parse_prefix(blob[: _PREFIX.size])
    mstart = _PREFIX.size
    pstart = mstart + meta_len
    return blob[mstart:pstart], blob[pstart:pstart + payload_len]
