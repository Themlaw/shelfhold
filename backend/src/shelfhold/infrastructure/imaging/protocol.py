"""The framing between the parent and the decode worker: stdlib only, so the worker stays light.

A frame is `>II` (JSON header length, payload length), the JSON header, the payload.
"""

from __future__ import annotations

import json
import struct
from typing import IO, Any, Final

MAX_HEADER_BYTES: Final = 64 * 1024
_FRAME: Final = struct.Struct(">II")


def encode_frame(header: dict[str, Any], payload: bytes = b"") -> bytes:
    raw = json.dumps(header, separators=(",", ":")).encode()
    return _FRAME.pack(len(raw), len(payload)) + raw + payload


def decode_frame(blob: bytes, max_payload: int) -> tuple[dict[str, Any], bytes]:
    if len(blob) < _FRAME.size:
        raise ValueError("short frame")
    header_len, payload_len = _FRAME.unpack_from(blob)
    if header_len > MAX_HEADER_BYTES or payload_len > max_payload:
        raise ValueError("frame too large")
    if len(blob) != _FRAME.size + header_len + payload_len:
        raise ValueError("frame length mismatch")
    header = json.loads(blob[_FRAME.size : _FRAME.size + header_len])
    if not isinstance(header, dict):
        raise ValueError("frame header is not an object")
    return header, blob[_FRAME.size + header_len :]


def read_frame(stream: IO[bytes], max_payload: int) -> tuple[dict[str, Any], bytes]:
    head = stream.read(_FRAME.size)
    if len(head) != _FRAME.size:
        raise ValueError("short frame")
    header_len, payload_len = _FRAME.unpack(head)
    if header_len > MAX_HEADER_BYTES or payload_len > max_payload:
        raise ValueError("frame too large")
    return decode_frame(head + stream.read(header_len + payload_len), max_payload)
