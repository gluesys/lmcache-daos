"""v2 on-disk object format: page-aligned payload for GPU-direct I/O.

v1 (``serde.py``) is ``[8-byte prefix][meta][payload]`` -- the payload starts at
an unaligned offset, which is fine for host reads but wrong for a payload that
is DMA'd straight into GPU memory (``dfs_read_gpu``): registration and RDMA
want the data region to start on a page boundary and to be the *only* thing in
the transfer.

v2 lays each object out as::

    offset 0      header page (HEADER_SIZE = 4096 bytes)
                  magic(8) version(u32) state(u32) meta_len(u32) reserved(u32)
                  payload_len(u64) header_crc(u32) meta[meta_len] zero-fill
    offset 4096   payload  (payload_len bytes, DMA target)

Rules:
- ``state`` is ``STATE_WRITING`` while the writer is still working and
  ``STATE_COMMITTED`` only after the payload is fully written. A reader treats
  anything but a committed header with a matching CRC as a miss -- exactly the
  torn-object policy v1 enforces by length checks (tests/test_torn_object.py),
  now enforced by a flag plus CRC so a payload of the right length but stale
  contents cannot pass.
- Writers publish atomically: write payload and header to a temporary name,
  then rename onto the final name (``dfs_move``). Readers therefore never see a
  half-written object under its final name; the state flag is defence in depth
  for writers that cannot rename.
- The CRC covers the header fields and the metadata, **not** the payload --
  a payload CRC would force the host to read GPU-bound data. Payload integrity
  is the transport's job (RoCE ICRC) and the length check's.
- v2 lives in its own namespace (``/v2/<sha256>``) beside v1 (``/<sha256>``) so
  the two paths can coexist on one container while a deployment migrates.

No LMCache or DAOS dependency: unit-testable anywhere (tests/test_serde_v2.py).
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass
from typing import Tuple

MAGIC = b"LMDGDS2\x00"
VERSION = 2
HEADER_SIZE = 4096                     # one host page; payload starts here
STATE_WRITING = 0
STATE_COMMITTED = 1

# magic(8s) version(I) state(I) meta_len(I) reserved(I) payload_len(Q) crc(I)
_FIXED = struct.Struct("<8sIIIIQI")
FIXED_SIZE = _FIXED.size               # 36
MAX_META = HEADER_SIZE - FIXED_SIZE    # 4060 bytes of metadata fit in the page
V2_PREFIX = "/v2"


class BadHeader(ValueError):
    """The header page is not a committed, intact v2 header (=> cache miss)."""


@dataclass(frozen=True)
class Header:
    state: int
    meta: bytes
    payload_len: int

    @property
    def committed(self) -> bool:
        return self.state == STATE_COMMITTED

    @property
    def total_len(self) -> int:
        return HEADER_SIZE + self.payload_len


def _crc(state: int, meta: bytes, payload_len: int) -> int:
    fixed = struct.pack("<8sIIIIQ", MAGIC, VERSION, state, len(meta), 0, payload_len)
    return zlib.crc32(meta, zlib.crc32(fixed)) & 0xFFFFFFFF


def pack_header(meta: bytes, payload_len: int, state: int = STATE_COMMITTED) -> bytes:
    """Build the full HEADER_SIZE page (zero-padded)."""
    if len(meta) > MAX_META:
        raise ValueError(f"metadata too large for v2 header: {len(meta)} > {MAX_META}")
    if payload_len < 0:
        raise ValueError("negative payload_len")
    if state not in (STATE_WRITING, STATE_COMMITTED):
        raise ValueError(f"bad state {state}")
    fixed = _FIXED.pack(MAGIC, VERSION, state, len(meta), 0, payload_len,
                        _crc(state, meta, payload_len))
    page = fixed + meta
    return page + b"\x00" * (HEADER_SIZE - len(page))


def parse_header(page: bytes, require_committed: bool = True) -> Header:
    """Validate a header page. Raises BadHeader on anything a reader must
    treat as a miss: short read, wrong magic/version, CRC mismatch, or (by
    default) an uncommitted state."""
    if len(page) < FIXED_SIZE:
        raise BadHeader(f"short header: {len(page)} < {FIXED_SIZE}")
    magic, version, state, meta_len, _res, payload_len, crc = _FIXED.unpack(page[:FIXED_SIZE])
    if magic != MAGIC:
        raise BadHeader("bad magic")
    if version != VERSION:
        raise BadHeader(f"unsupported version {version}")
    if meta_len > MAX_META:
        raise BadHeader(f"meta_len {meta_len} exceeds header page")
    if len(page) < FIXED_SIZE + meta_len:
        raise BadHeader("short header: metadata truncated")
    meta = bytes(page[FIXED_SIZE:FIXED_SIZE + meta_len])
    if _crc(state, meta, payload_len) != crc:
        raise BadHeader("header CRC mismatch")
    if require_committed and state != STATE_COMMITTED:
        raise BadHeader("object not committed")
    return Header(state=state, meta=meta, payload_len=payload_len)


def payload_offset() -> int:
    return HEADER_SIZE


def check_payload_len(hdr: Header, got: int) -> bool:
    """True iff a payload read returned exactly what the header promised."""
    return got == hdr.payload_len


def key_to_path(key) -> str:
    """v2 namespace, same flat sha256 rule as v1's ``_key_to_path``.

    Deliberately NOT LMCache GdsBackend's ``str(chunk_hash)[:2]/[2:4]`` rule:
    chunk_hash is a signed int there, which yields directory names like ``-1``.
    """
    s = key.to_string() if hasattr(key, "to_string") else str(key)
    return f"{V2_PREFIX}/{hashlib.sha256(s.encode()).hexdigest()}"


def temp_path(final: str, nonce: str) -> str:
    """Temporary name for the atomic publish: same directory, hidden prefix."""
    d, _, name = final.rpartition("/")
    return f"{d}/.tmp-{name}-{nonce}"


def split_v1_blob(blob: bytes) -> Tuple[bytes, bytes]:
    """Helper for migration tests: v1 blob -> (meta, payload)."""
    from . import serde  # local import keeps this module dependency-free at import
    return serde.unpack(blob)
