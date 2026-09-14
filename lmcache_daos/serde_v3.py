# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""v3 layout: one dkey per chunk, metadata and payload in separate akeys.

v1 and v2 are *file* layouts -- a byte range in a DFS object, with the header at
offset 0. v3 is not a new header format; it is a new **placement**. The bytes in
the metadata akey are exactly a v2 header page with the zero padding removed,
and `parse_meta` below delegates to `serde_v2.parse_header` unchanged.

That reuse is deliberate rather than lazy. The torn-object policy this project
cares about -- a partially written object must read back as a miss, never as
garbage -- is enforced by v2's magic/version/state/CRC discipline, and
tests/test_serde_v2.py already proves it for every truncation shape. Writing a
second header format for v3 would mean proving all of that again, for no gain:
the two can never be confused, because a v2 object lives in a POSIX container
and a v3 object cannot (see doc/RAW-API-PLAN.md -- container layout is fixed at
creation and each API rejects the other's container).

Layout::

    dkey  = sha256(key.to_string())        raw 32 bytes, not hex
    akey  "M"                              header: fixed(36) + meta, unpadded
    akey  "P"                              payload            (whole-chunk mode)
    akey  "L000".."L999"                   payload per layer  (layerwise mode)

Two properties follow from the placement, and they are the point of v3:

1. **No padding.** v2 pads its header to a 4096-byte page because the payload
   that follows must start page-aligned for GPU-direct DMA. In v3 the payload is
   a different akey, so there is nothing to align against and nothing to pad.

2. **No rename.** v2 publishes atomically by writing to a temporary name and
   renaming (dfs_move); dkey/akey has no rename. It does not need one: the
   metadata akey IS the commit record. Write the payload akey(s) first, then the
   metadata akey. A crash between them leaves a dkey whose payload is present
   and whose metadata is absent, and a reader that finds no metadata reports a
   miss -- which is the same answer v1 gives for a truncated file.

   Writing both in ONE daos_obj_update() would be tempting and is not done here.
   DAOS applies an update atomically per shard, but a dkey's akeys can land on
   different shards under replication or EC, so a single call is not a single
   commit point in general. Ordering costs one extra RPC against a metadata akey
   of a few dozen bytes; a wrong commit point costs silent corruption, and this
   project has already paid for one of those.

   Overwrite is safe for the same key because LMCache keys are content-addressed
   (`chunk_hash`), so re-storing a key re-stores identical bytes.

No DAOS and no LMCache dependency: unit-testable anywhere
(tests/test_serde_v3.py).
"""

from __future__ import annotations

import hashlib
from typing import Tuple

from .serde_v2 import (  # noqa: F401  (re-exported on purpose, see module docstring)
    BadHeader,
    FIXED_SIZE,
    Header,
    MAX_META,
    STATE_COMMITTED,
    STATE_WRITING,
    _FIXED,
    _crc,
    parse_header,
)

AKEY_META = b"M"
AKEY_PAYLOAD = b"P"

# 3 digits, so "L010" sorts after "L009" when dkeys are enumerated. Layer counts
# are in the tens (Qwen3-14B has 40); a model past 1000 layers needs a wider
# field, and akey_layer() refuses rather than silently colliding L1000 with
# something else.
_MAX_LAYER = 999


def dkey_for(key) -> bytes:
    """One dkey per chunk.

    Raw digest, not hex: a dkey is arbitrary binary, so the 64-character hex
    string v1 needed for a filesystem path would double the key size for
    nothing. ``key.to_string()`` is used rather than the object itself because
    that is what v1 and v2 hash, so the same logical key maps to corresponding
    names in all three layouts -- which makes a migration comparable.
    """
    s = key.to_string() if hasattr(key, "to_string") else str(key)
    return hashlib.sha256(s.encode()).digest()


def akey_layer(i: int) -> bytes:
    """Payload akey for layer ``i`` in layerwise mode."""
    if not 0 <= i <= _MAX_LAYER:
        raise ValueError(f"layer {i} outside 0..{_MAX_LAYER}")
    return b"L%03d" % i


def pack_meta(meta: bytes, payload_len: int, state: int = STATE_COMMITTED) -> bytes:
    """The metadata akey's contents: a v2 header with no page padding.

    Same fields, same CRC, same bytes as ``serde_v2.pack_header`` up to
    ``FIXED_SIZE + len(meta)``; only the zero fill is dropped.
    """
    if len(meta) > MAX_META:
        raise ValueError(f"metadata too large: {len(meta)} > {MAX_META}")
    if payload_len < 0:
        raise ValueError("negative payload_len")
    if state not in (STATE_WRITING, STATE_COMMITTED):
        raise ValueError(f"bad state {state}")
    from .serde_v2 import MAGIC, VERSION

    return _FIXED.pack(MAGIC, VERSION, state, len(meta), 0, payload_len,
                       _crc(state, meta, payload_len)) + meta


def parse_meta(buf: bytes, require_committed: bool = True) -> Header:
    """Validate the metadata akey. Raises BadHeader on anything a reader must
    treat as a miss -- delegated to v2 so the two cannot drift apart."""
    return parse_header(buf, require_committed=require_committed)


def meta_len_for(meta: bytes) -> int:
    """How many bytes ``pack_meta`` will produce, for sizing a fetch."""
    return FIXED_SIZE + len(meta)


def payload_akeys(n_layers: int) -> Tuple[bytes, ...]:
    """Payload akeys for a chunk: one, or one per layer.

    ``n_layers <= 0`` means whole-chunk mode. The distinction is the whole point
    of v3 for layerwise: the layers of one chunk share a dkey, so they can be
    fetched in a single RPC with N iods -- which is where the 2.4x folding in
    the NIXL measurements came from (doc/NIXL-DAOS-MEASUREMENT.md). Chunks
    cannot be folded that way; see doc/RAW-API-PLAN.md.
    """
    if n_layers <= 0:
        return (AKEY_PAYLOAD,)
    return tuple(akey_layer(i) for i in range(n_layers))
