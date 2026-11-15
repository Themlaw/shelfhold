"""The bytes of a block, a chunk at a time (ADR-0053 §8; #288 MIN-1).

Memory is one chunk, whatever the size: a page is read from its chapter's archive in pieces of
`chunk_bytes`, its CRC-32 is computed on the way and compared with the central directory's at the
end of the entry, and the headers come from `BundleZip`. Every file was opened and checked before
the first byte, and the descriptors stay open: a replacement by `rename` leaves the old inode
readable and the stream coherent. A file overwritten *in place* cannot be protected by a
descriptor, so the CRC catches it and the stream stops (`BundleChangedError`) instead of sending
two contents under valid headers.

Before each chunk the token is looked at again (revoked or expired: the transfer stops at the next
block of bytes), at each chapter and every `visibility_every_bytes` the reader's scope is read
again (a ceiling lowered, an Edition reclassified), and a reader that takes less than
`min_rate_bps` over `rate_window_s` is cut: a stalled e-reader cannot hold a slot for ever.
"""

import time
import zlib
from collections.abc import Callable, Generator, Iterator, Sequence
from typing import IO

from shelfhold.infrastructure.archives import UnsafeArchiveError
from shelfhold.reading.bundle.errors import BundleChangedError, BundleGoneError
from shelfhold.reading.bundle.limits import BundleLimits
from shelfhold.reading.bundle.sources import OpenSource, Page
from shelfhold.reading.domain.bundle_zip import BundleZip

EntryRef = tuple[int, Page]  # (index of the source in `sources`, the page)


class BundleStream:
    """An iterator of `bytes` with a `close()`: what `StreamingResponse` iterates."""

    def __init__(
        self,
        layout: BundleZip,
        sources: Sequence[OpenSource],
        entries: Sequence[EntryRef],
        starts: set[int],
        *,
        limits: BundleLimits,
        token_alive: Callable[[], bool],
        still_visible: Callable[[], bool],
        release: Callable[[], None],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._layout, self._sources, self._entries = layout, sources, entries
        self._starts = starts  # indexes of the entries that begin a chapter
        self._limits = limits
        self._alive, self._visible = token_alive, still_visible
        self._release_slot = release
        self._monotonic = monotonic
        self._done = False
        self._generator = self._run()

    def __iter__(self) -> "BundleStream":
        return self

    def __next__(self) -> bytes:
        if self._done:
            raise StopIteration
        try:
            return next(self._generator)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Give back the slot and the descriptors; harmless to call twice."""
        if self._done:
            return
        self._done = True
        try:
            self._generator.close()
        finally:
            for source in self._sources:
                source.close()
            self._release_slot()

    def __del__(self) -> None:
        if getattr(self, "_done", True) is False:
            self.close()

    # -- the stream -----------------------------------------------------------------------------
    def _run(self) -> Generator[bytes]:
        limits = self._limits
        window_start, window_bytes, since_visible = self._monotonic(), 0, 0
        for piece in self._layout.pieces():
            if isinstance(piece, bytes):
                parts: Iterator[bytes] = self._slices(piece)
            else:
                if piece in self._starts and not self._visible():
                    raise BundleGoneError("the reader may no longer see this block")
                parts = self._page(piece)
            for data in parts:
                if not self._alive():
                    raise BundleGoneError("the token is no longer valid")
                yield data
                window_bytes += len(data)
                since_visible += len(data)
                now = self._monotonic()
                if now - window_start >= limits.rate_window_s:
                    if window_bytes / (now - window_start) < limits.min_rate_bps:
                        raise BundleGoneError("the reader is too slow")
                    window_start, window_bytes = now, 0
                if since_visible >= limits.visibility_every_bytes:
                    since_visible = 0
                    if not self._visible():
                        raise BundleGoneError("the reader may no longer see this block")

    def _slices(self, data: bytes) -> Iterator[bytes]:
        step = self._limits.chunk_bytes
        for start in range(0, len(data), step):
            yield data[start : start + step]

    def _page(self, index: int) -> Iterator[bytes]:
        source_index, page = self._entries[index]
        expected = self._layout.entries[index]
        crc, sent = 0, 0
        stream: IO[bytes] | None = None
        try:
            stream = self._sources[source_index].page_reader(page)
            while sent < expected.size:
                try:
                    data = stream.read(min(self._limits.chunk_bytes, expected.size - sent))
                except (UnsafeArchiveError, zlib.error, EOFError, OSError) as error:
                    raise BundleChangedError("a chapter no longer holds what was read") from error
                if not data:
                    raise BundleChangedError("a chapter is shorter than it was")
                crc = zlib.crc32(data, crc)
                sent += len(data)
                yield data
        except (UnsafeArchiveError, zlib.error, EOFError, OSError) as error:
            raise BundleChangedError("a chapter changed during the transfer") from error
        finally:
            if stream is not None:
                stream.close()
        if crc != expected.crc32:
            raise BundleChangedError("a chapter changed during the transfer")
