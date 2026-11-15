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
`min_rate_bps` over `rate_window_s` is cut. That measure runs only when the stream is asked for
the next chunk, so it cannot catch a client that stops reading altogether: the lease (`gate.py`)
expires on no progress and stops the stream at its next call.

The generator holds no reference to the `BundleStream`: dropped by the server, the stream is freed
at once (no cycle for the garbage collector to find) and gives its slot and descriptors back; the
route also closes it as the client goes away.
"""

import threading
import time
import zlib
from collections.abc import Callable, Generator, Iterator, Sequence
from typing import IO

from shelfhold.infrastructure.archives import UnsafeArchiveError
from shelfhold.reading.bundle.errors import BundleChangedError, BundleGoneError
from shelfhold.reading.bundle.gate import Lease
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
        lease: Lease,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sources = sources
        self._lease = lease
        lease.hold(sources)
        self._lock = threading.Lock()
        self._done = False
        self._generator = _run(
            layout, sources, entries, starts, limits, token_alive, still_visible, lease, monotonic
        )

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
        """Give back the slot and the descriptors; harmless to call twice, from any thread."""
        with self._lock:
            if self._done:
                return
            self._done = True
        try:
            self._generator.close()
        except ValueError:  # a thread is inside `next`: its own failure path closes the rest
            pass
        finally:
            for source in self._sources:
                source.close()
            self._lease.release()

    def __del__(self) -> None:
        if getattr(self, "_done", True) is False:
            self.close()


# -- the stream -----------------------------------------------------------------------------------
def _run(
    layout: BundleZip,
    sources: Sequence[OpenSource],
    entries: Sequence[EntryRef],
    starts: set[int],
    limits: BundleLimits,
    token_alive: Callable[[], bool],
    still_visible: Callable[[], bool],
    lease: Lease,
    monotonic: Callable[[], float],
) -> Generator[bytes]:
    window_start, window_bytes, since_visible = monotonic(), 0, 0
    began = window_start
    for piece in layout.pieces():
        if isinstance(piece, bytes):
            parts: Iterator[bytes] = _slices(piece, limits.chunk_bytes)
        else:
            if piece in starts and not still_visible():
                raise BundleGoneError("the reader may no longer see this block")
            parts = _page(layout, sources, entries, piece, limits.chunk_bytes)
        for data in parts:
            if lease.lost:
                raise BundleGoneError("the slot was taken back: no progress for too long")
            if not token_alive():
                raise BundleGoneError("the token is no longer valid")
            if monotonic() - began > limits.max_duration_s:
                raise BundleGoneError("the transfer lasts longer than it may")
            lease.progress()
            yield data
            lease.progress()  # asked for the next chunk: the previous one was taken
            if lease.lost:
                raise BundleGoneError("the slot was taken back: no progress for too long")
            window_bytes += len(data)
            since_visible += len(data)
            now = monotonic()
            if now - window_start >= limits.rate_window_s:
                if window_bytes / (now - window_start) < limits.min_rate_bps:
                    raise BundleGoneError("the reader is too slow")
                window_start, window_bytes = now, 0
            if since_visible >= limits.visibility_every_bytes:
                since_visible = 0
                if not still_visible():
                    raise BundleGoneError("the reader may no longer see this block")


def _slices(data: bytes, step: int) -> Iterator[bytes]:
    for start in range(0, len(data), step):
        yield data[start : start + step]


def _page(
    layout: BundleZip,
    sources: Sequence[OpenSource],
    entries: Sequence[EntryRef],
    index: int,
    chunk_bytes: int,
) -> Iterator[bytes]:
    source_index, page = entries[index]
    expected = layout.entries[index]
    crc, sent = 0, 0
    stream: IO[bytes] | None = None
    try:
        stream = sources[source_index].page_reader(page)
        while sent < expected.size:
            try:
                data = stream.read(min(chunk_bytes, expected.size - sent))
            except (UnsafeArchiveError, zlib.error, EOFError, OSError) as error:
                raise BundleChangedError("a chapter no longer holds what was read") from error
            if not data:
                raise BundleChangedError("a chapter is shorter than it was")
            crc = zlib.crc32(data, crc)
            sent += len(data)
            yield data
    except BundleGoneError:
        raise
    except (UnsafeArchiveError, zlib.error, EOFError, OSError) as error:
        raise BundleChangedError("a chapter changed during the transfer") from error
    except ValueError as error:  # the archive was closed under us: the slot was taken back
        raise BundleGoneError("the transfer was closed") from error
    finally:
        if stream is not None:
            stream.close()
    if crc != expected.crc32:
        raise BundleChangedError("a chapter changed during the transfer")
