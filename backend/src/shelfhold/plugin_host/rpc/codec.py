"""Frames: a 4-byte big-endian length, then that many bytes of UTF-8 JSON (D12).

Never a line: the length is read first and checked before anything is allocated, so a peer cannot
make this end hold more than ``MAX_MESSAGE_BYTES``. Decoding is strict: JSON types only, no ``NaN``
or ``Infinity``, no duplicate key, no nesting deeper than ``MAX_DEPTH``. Nothing that is not plain
data crosses (`to_wire`): a live object has no JSON form, and none is guessed.

Imports the standard library, pydantic and nothing else: it runs in the plugin's subprocess.
"""

import asyncio
import base64
import json
import struct
from typing import Any, Final

from pydantic import BaseModel

MAX_MESSAGE_BYTES: Final = 8 * 1024 * 1024
MAX_DEPTH: Final = 64
_HEADER: Final = struct.Struct(">I")


class FrameError(ValueError):
    """A frame the protocol does not allow: oversized, cut short, not strict JSON."""


class FrameTooLarge(FrameError):
    """A frame, announced or built, of more than ``MAX_MESSAGE_BYTES``."""


class LiveObjectError(FrameError):
    """A value with no plain JSON form was about to cross: a live object never does."""


def _no_constant(name: str) -> Any:
    raise FrameError(f"{name} is not JSON")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise FrameError(f"duplicate key {key[:40]!r}")
        out[key] = value
    return out


def _depth(value: Any) -> int:
    deepest, stack = 0, [(value, 1)]
    while stack:
        item, level = stack.pop()
        if isinstance(item, dict | list):
            if level > MAX_DEPTH:
                return level
            deepest = max(deepest, level)
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, level + 1) for child in children)
    return deepest


def decode_payload(payload: bytes) -> Any:
    """The JSON value of one frame's payload, or ``FrameError``."""
    try:
        value = json.loads(
            payload.decode("utf-8"), parse_constant=_no_constant, object_pairs_hook=_no_duplicates
        )
    except FrameError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise FrameError(f"not valid JSON: {type(exc).__name__}") from exc
    if _depth(value) > MAX_DEPTH:
        raise FrameError(f"nested deeper than {MAX_DEPTH}")
    return value


def to_wire(value: Any, _level: int = 0) -> Any:
    """`value` as plain JSON data: SDK models by their JSON form, `bytes` as base64, and anything
    else only if it already is a dict with text keys, a list, a string, a number, a boolean or
    ``None``. A tuple is a list; a set, a callable, a file, a socket, a lock is a
    ``LiveObjectError``."""
    if _level > MAX_DEPTH:
        raise LiveObjectError("nested too deep")
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise LiveObjectError("NaN and Infinity are not JSON")
        return value
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, BaseModel):
        return to_wire(value.model_dump(mode="json"), _level + 1)
    if isinstance(value, list | tuple):
        return [to_wire(item, _level + 1) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise LiveObjectError("a JSON object has text keys")
        return {key: to_wire(item, _level + 1) for key, item in value.items()}
    raise LiveObjectError(f"{type(value).__name__} is a live object, not data")


def encode_frame(message: Any) -> bytes:
    """One frame for `message`, which must already be plain data (`to_wire`)."""
    try:
        body = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise LiveObjectError(f"not plain data: {exc}") from exc
    payload = body.encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise FrameTooLarge(f"{len(payload)} bytes: a message holds at most {MAX_MESSAGE_BYTES}")
    return _HEADER.pack(len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> bytes | None:
    """The payload of the next frame; ``None`` at a clean end (between two frames)."""
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise FrameError("the stream ended inside a frame header") from exc
    (length,) = _HEADER.unpack(header)
    if length > MAX_MESSAGE_BYTES:  # decided on the header: no byte of the body is read or held
        raise FrameTooLarge(f"a frame announces {length} bytes, the most is {MAX_MESSAGE_BYTES}")
    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise FrameError("the stream ended inside a frame") from exc


async def write_frame(writer: Any, message: Any) -> None:
    writer.write(encode_frame(message))
    await writer.drain()
