"""The hashes KOReader and Kavita identify a document by (ENF-QUAL-005).

Both hash a few samples of the file, never the whole of it, so the digest survives data appended
at the tail (KOReader's reason). Two sources, read before this was written:

* KOReader, `util.partialMD5`: koreader/koreader, `frontend/util.lua` lines 1094-1112, file last
  changed by commit 78d586b420835e85cacf4fc72f1a7e7a40741afd::

      for i = -1, 10 do
          file:seek("set", lshift(step, 2*i))     -- step = size = 1024
          local sample = file:read(size)
          if sample then update(sample) else break end
      end

  12 samples of at most 1 KiB; the digest is lowercase hex.
* Kavita, `KoreaderHelper.HashContents`: Kareadita/Kavita,
  `Kavita.Services/Helpers/KoreaderHelper.cs` lines 38-70 (develop e844b64f2d64; file last
  changed by c62b20f54bec; the same loop is at lines 52-54 of the v0.9.1.4 tag)::

      for (var i = -1; i < 10; i++) {
          file.Position = step << 2 * i;
          var bytesRead = file.Read(buffer, 0, size);
          if (bytesRead > 0) md5.TransformBlock(buffer, 0, bytesRead, buffer, 0); else break;
      }
      ... Convert.ToHexString(md5.Hash).ToUpper();

How they differ, exactly: Kavita stops at `i = 9` (11 samples, the last at 256 MiB) where KOReader
goes on to `i = 10` (a 12th sample at 1 GiB), and Kavita's hex is uppercase. The digests agree
for every file of at most 1 GiB (the 12th sample then reads nothing) and differ for every file
with a byte at offset 1 GiB. Same sampling, same stop at the first empty read, same partial hash of
a short last sample.

The first offset is 0, not 256. `lshift(1024, -2)` (LuaJIT's `bit.lshift`) and `1024 << -2` (C#
`int`) both take the count modulo 32, that is 30, and `1024 << 30` is 2**40, which is 0 on 32 bits.
`_offset` keeps that arithmetic instead of writing the result down, and a test pins the offsets to
what the reference code was seen doing.

The input is a `pathlib.Path` or a seekable binary stream, never a `str` that could be a path or a
content. Only the samples are read.
"""

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

SAMPLE_SIZE = 1024
_STEP = 1024
_WORD = 0xFFFFFFFF  # the shifts of both sources are 32-bit


def _offset(i: int) -> int:
    """`lshift(step, 2*i)` (LuaJIT `bit.lshift`) and `step << 2 * i` (C# `int`): the count is
    masked to 5 bits and the result kept on 32 bits. For `i = -1` that is 0."""
    return (_STEP << ((2 * i) & 31)) & _WORD


KOREADER_OFFSETS: Sequence[int] = tuple(_offset(i) for i in range(-1, 11))  # `for i = -1, 10`
KAVITA_OFFSETS: Sequence[int] = tuple(_offset(i) for i in range(-1, 10))  # `i < 10`


def _read_sample(stream: BinaryIO) -> bytes:
    """Up to `SAMPLE_SIZE` bytes: a short answer in the middle of the file is asked again, as the
    C library behind both sources does, so that only the end of the file shortens a sample."""
    chunks: list[bytes] = []
    missing = SAMPLE_SIZE
    while missing:
        chunk = stream.read(missing)
        if not chunk:
            break
        chunks.append(chunk)
        missing -= len(chunk)
    return b"".join(chunks)


def _hexdigest(source: Path | BinaryIO, offsets: Sequence[int]) -> str:
    if isinstance(source, Path):
        with source.open("rb") as stream:
            return _hexdigest(stream, offsets)
    if not (hasattr(source, "seek") and hasattr(source, "read")):
        raise TypeError("a Path or a seekable binary stream is expected, not a content or a str")
    md5 = hashlib.md5(usedforsecurity=False)
    for offset in offsets:
        source.seek(offset)
        sample = _read_sample(source)
        if not sample:
            break
        md5.update(sample)
    return md5.hexdigest()


def koreader_partial_md5(source: Path | BinaryIO) -> str:
    """KOReader's `util.partialMD5`: lowercase hex, 12 samples at 0, 1 Ki, 4 Ki ... 1 Gi."""
    return _hexdigest(source, KOREADER_OFFSETS)


def kavita_hash(source: Path | BinaryIO) -> str:
    """Kavita's `KoreaderHelper.HashContents`: uppercase hex, 11 samples (none at 1 Gi)."""
    return _hexdigest(source, KAVITA_OFFSETS).upper()


def verified_hashes(stream: BinaryIO) -> tuple[str, str, str]:
    """SHA-256, KOReader's partial MD5 and Kavita's, **in one pass** over one open file.

    Returns `(sha256_hex, koreader_hex, kavita_hex)`, the last two lowercase. The sample offsets
    are known beforehand, so the samples are taken from the very blocks that feed the SHA-256: the
    bytes that were sampled are, by construction, the bytes that were verified, whatever happens
    to the file while it is read. Equal to `koreader_partial_md5` and `kavita_hash` (lowercased)
    for the same bytes, `test_one_pass_hashes_equal_the_two_sources` proves it.
    """
    wanted = sorted({*KOREADER_OFFSETS, *KAVITA_OFFSETS})
    taken: dict[int, bytearray] = {offset: bytearray() for offset in wanted}
    sha = hashlib.sha256()
    position = 0
    while block := stream.read(1 << 20):
        end = position + len(block)
        for offset in wanted:
            if offset >= end:
                break
            sample = taken[offset]
            have = len(sample)
            if have < SAMPLE_SIZE and offset + have < end:
                begin = max(offset + have, position)
                sample += block[begin - position : min(offset + SAMPLE_SIZE, end) - position]
        sha.update(block)
        position = end

    def digest(offsets: Sequence[int]) -> str:
        md5 = hashlib.md5(usedforsecurity=False)
        for offset in offsets:
            if not taken[offset]:
                break
            md5.update(bytes(taken[offset]))
        return md5.hexdigest()

    return sha.hexdigest(), digest(KOREADER_OFFSETS), digest(KAVITA_OFFSETS)
