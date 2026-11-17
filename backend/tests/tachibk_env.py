"""A synthetic Mihon backup, built by hand (M4 L26, EF-INT-008).

Only invented names and numbers: no file taken from a real backup, no real source. A backup is the
gzip of a protobuf message; the encoder below writes just the fields Shelfhold reads, and lets a
test add any other field or any malformed byte, so that a forged file is a few lines.

Field numbers are those the format documents: `Backup` (1: manga, 2: categories, 100: sources,
104: preferences), `BackupManga` (1: source, 2: url, 3: title, 16: chapters, 18: tracking,
100: favorite), `BackupChapter` (1: url, 2: name, 4: read, 6: last page read, 9: number),
`BackupTracking` (1: tracker, 4: url, 5: title, 6: last chapter read, 100: remote id).
"""

from __future__ import annotations

import gzip
import secrets
import struct
import zlib
from collections.abc import Iterable

SOURCE_NAME = "SynthSource-Xq7"  # an invented source name: it must be found nowhere
SOURCE_ID = 4_242_424_242
OTHER_SOURCE_ID = 5_151
SECRET = "tok-" + secrets.token_hex(6)  # what a URL or a preference might hold; drawn per run


def varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def vi(field: int, value: int) -> bytes:
    return tag(field, 0) + varint(value)


def ld(field: int, payload: bytes | str) -> bytes:
    raw = payload.encode() if isinstance(payload, str) else payload
    return tag(field, 2) + varint(len(raw)) + raw


def f32(field: int, value: float) -> bytes:
    return tag(field, 5) + struct.pack("<f", value)


def chapter(
    url: str, number: float | None, *, read: bool | None = None, page: int | None = None,
    extra: bytes = b"",
) -> bytes:  # fmt: skip
    body = ld(1, url) + ld(2, f"Chapter {number}")
    if read is not None:
        body += vi(4, int(read))
    if page is not None:
        body += vi(6, page)
    if number is not None:
        body += f32(9, number)
    return body + extra


def tracking(
    tracker: int, remote_id: int, *, last: float | None = None, url: str | None = None,
    extra: bytes = b"",
) -> bytes:  # fmt: skip
    body = vi(1, tracker) + vi(100, remote_id)
    if last is not None:
        body += f32(6, last)
    if url is not None:
        body += ld(4, url) + ld(5, "A tracked title")
    return body + extra


def manga(
    source: int, url: str, title: str, chapters: Iterable[bytes] = (),
    trackers: Iterable[bytes] = (), *, favorite: bool | None = None, extra: bytes = b"",
) -> bytes:  # fmt: skip
    body = vi(1, source) + ld(2, url) + ld(3, title)
    for item in chapters:
        body += ld(16, item)
    for item in trackers:
        body += ld(18, item)
    if favorite is not None:
        body += vi(100, int(favorite))
    return body + extra


def backup(
    mangas: Iterable[bytes], *, sources: Iterable[tuple[int, str]] = ((SOURCE_ID, SOURCE_NAME),),
    extra: bytes = b"",
) -> bytes:  # fmt: skip
    body = b"".join(ld(1, item) for item in mangas)
    for source_id, name in sources:
        body += ld(100, ld(1, name) + vi(2, source_id))
    return body + extra


def tachibk(message: bytes) -> bytes:
    """The file: one gzip member holding the protobuf message."""
    return gzip.compress(message, mtime=0)


def sample() -> bytes:
    """Two series on two sources: one with read chapters, a partial one, a tracker; one empty."""
    first = manga(
        SOURCE_ID, "/series/orchard-tales", "Orchard Tales",
        chapters=[
            chapter("/c/1", 1.0, read=True),
            chapter("/c/2", 2.0, read=True),
            chapter("/c/3", 3.0, read=False, page=7),  # started, never finished
            chapter("/c/4", 4.0, read=False),  # untouched
            chapter("/c/5", 5.5),  # no flag at all: not proven read
        ],
        trackers=[tracking(2, 7_001, last=40.0, url=f"https://tracker.example/x?token={SECRET}")],
        extra=ld(77, "a field of a later version") + vi(78, 5),  # unknown fields, ignored
    )  # fmt: skip
    second = manga(OTHER_SOURCE_ID, "/series/harbor", "Harbor Notes", favorite=True)
    return tachibk(
        backup(
            [first, second],
            sources=[(SOURCE_ID, SOURCE_NAME), (OTHER_SOURCE_ID, "SynthSource-Other")],
            extra=ld(104, f"oauth={SECRET}"),  # the preferences, which hold tracker tokens
        )
    )


def bomb(megabytes: int) -> bytes:
    """A gzip of zeros: a few kilobytes that expand to `megabytes` MiB."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
    chunk = bytes(1 << 20)
    out = b"".join(compressor.compress(chunk) for _ in range(megabytes))
    return out + compressor.flush()


def nested_groups(depth: int) -> bytes:
    """`depth` start-group tags then as many end-group tags: a recursion a skipper must bound."""
    return b"".join(tag(9, 3) for _ in range(depth)) + b"".join(tag(9, 4) for _ in range(depth))


def nested_messages(depth: int) -> bytes:
    """A manga inside a manga inside a manga, `depth` times: the recursive forgery."""
    inner = vi(1, 1)
    for _ in range(depth):
        inner = ld(1, inner)
    return inner
