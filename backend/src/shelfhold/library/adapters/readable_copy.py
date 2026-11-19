"""The file `/content` reads for a carrier (M5 L05a, ADR-0055, ADR-0047 I35 extended).

The one rule: the reader is given what may leave Shelfhold, never an original.
- an EPUB: `servable_epub`, its verified neutralized derivative (or, for a `publisher` EPUB, the
  file as filed). An `untrusted` EPUB with no derivative, or whose library file is not the recorded
  derivative, serves nothing: there is no fallback to the original kept in `/store`;
- a CBZ: the library copy of the Unit (never `/store`), the first one the integrity check has not
  marked damaged;
- any other format, an absent carrier, an unknown carrier type: nothing.

A carrier is a `content` row, or an `export_item` that points at one. The path read is the one a row
and the replica registry name: nothing a client supplies is ever part of it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any

import sqlalchemy as sa

from shelfhold.infrastructure.archives import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.fsops import Fsops, FsopsError
from shelfhold.infrastructure.schema import export, storage
from shelfhold.library.adapters.epub_derivative import servable_epub
from shelfhold.library.adapters.epub_excerpt import epub_excerpt
from shelfhold.library.domain.failures import OriginalNotServableError
from shelfhold.library.replica import LibraryCopy, library_copies
from shelfhold.reading.application.content_access import (
    ExcerptUnavailableError,
    NoReadableCopyError,
)
from shelfhold.reading.domain.content_ticket import Scope
from shelfhold.reading.domain.content_types import media_type_of
from shelfhold.reading.domain.excerpt import page_excerpt
from shelfhold.reading.domain.publication import PAGE_TYPES

__all__ = ["ReadableCopies"]


class _Open:
    """An archive, whole or bounded to `allowed` (an excerpt): outside the bound a name is not
    there, for `names()` and for `read()`."""

    def __init__(
        self,
        stack: ExitStack,
        archive: Any,
        allowed: frozenset[str] | None = None,
        pin: str = "",
        stream: Any = None,
    ) -> None:
        self._stack, self._archive, self._allowed, self._pin = stack, archive, allowed, pin
        self._stream = stream

    def whole(self, max_bytes: int) -> bytes:
        """The file itself, for a full scope: an excerpt is never the whole file."""
        if self._allowed is not None or self._stream is None:
            raise ValueError("no whole file for an excerpt")
        self._stream.seek(0)
        data: bytes = self._stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("the file is larger than the bound")
        return data

    def fingerprint(self) -> str:
        return self._pin

    def names(self) -> frozenset[str]:
        every = frozenset(e.name for e in self._archive.entries if not e.is_dir)
        return every if self._allowed is None else every & self._allowed

    def read(self, name: str) -> bytes:
        if self._allowed is not None and name not in self._allowed:
            raise KeyError(name)
        return bytes(self._archive.read(name))

    def close(self) -> None:
        self._stack.close()


def _is_copy_of(copy: LibraryCopy, content_hash: str | None, stat: os.stat_result) -> bool:
    """The registry's copy is this content's: it is the file the registry recorded (size and
    modification time, always required) and, when the content has a hash, of that identity; never
    « the first copy of the Unit ». A file that is no longer the recorded one is refused."""
    if copy.size is None or copy.mtime_ns is None:
        return False  # a row that never recorded the file proves nothing
    if copy.size != stat.st_size or copy.mtime_ns != stat.st_mtime_ns:
        return False
    return content_hash is None or copy.content_hash == content_hash


class ReadableCopies:
    """The `ArchiveSource` of `reading.application.content_access`, over the library."""

    def __init__(self, engine: sa.Engine, fsops: Callable[[], Fsops]) -> None:
        self._engine, self._fsops = engine, fsops

    def open(self, carrier_type: str, carrier_id: str, scope: Scope = "full") -> _Open:
        content_id = self._content_id(carrier_type, carrier_id)
        if content_id is None:
            raise NoReadableCopyError
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    storage.content.c.id,
                    storage.content.c.unit_id,
                    storage.content.c.format,
                    storage.content.c.content_hash,
                ).where(storage.content.c.id == content_id)
            ).first()
            if row is None or row.unit_id is None:
                raise NoReadableCopyError
            copies = (
                []
                if row.format != "cbz"
                else [c for c in library_copies(connection, row.unit_id) if not c.damaged]
            )
            # a CBZ is read only from a copy that is proved to be this content (see `_is_copy_of`)
            copies = [c for c in copies if c.rel_path.lower().endswith(".cbz")]
        fsops = self._fsops()
        if row.format == "epub":
            try:
                served = servable_epub(self._engine, fsops, row.unit_id)
            except OriginalNotServableError:
                raise NoReadableCopyError from None
            if served.content_id != row.id:
                raise NoReadableCopyError
            paths: list[tuple[str, LibraryCopy | None]] = [(served.path, None)]
        elif row.format == "cbz":
            paths = [(c.local, c) for c in copies]
        else:
            raise NoReadableCopyError
        for path, copy in paths:
            stack = ExitStack()
            try:
                stream = stack.enter_context(fsops.open_read(path))
                stat = os.fstat(stream.fileno())
                if copy is not None and not _is_copy_of(copy, row.content_hash, stat):
                    stack.close()
                    continue
                archive = stack.enter_context(safe_open_archive(stream, name=path))
            except (OSError, FsopsError, UnsafeArchiveError):
                stack.close()
                continue
            pin = f"{row.id}|{row.content_hash}|{stat.st_size}|{stat.st_mtime_ns}"
            if scope == "full":
                return _Open(stack, archive, None, pin, stream)
            try:  # an excerpt: the list is the server's, computed from this very file
                allowed = self._excerpt(row.format, archive)
            except BaseException:
                stack.close()
                raise
            if allowed is None:
                stack.close()
                raise ExcerptUnavailableError
            return _Open(stack, archive, allowed, pin)
        raise NoReadableCopyError

    @staticmethod
    def _excerpt(kind: str, archive: Any) -> frozenset[str] | None:
        if kind == "epub":
            return epub_excerpt(archive)
        pages = page_excerpt(
            [
                e.name
                for e in archive.entries
                if not e.is_dir and media_type_of(e.name) in PAGE_TYPES
            ]
        )
        return None if pages is None else frozenset(pages)

    def _content_id(self, carrier_type: str, carrier_id: str) -> str | None:
        if carrier_type == "content":
            return carrier_id
        if carrier_type != "export_item":
            return None
        with self._engine.connect() as connection:
            found = connection.execute(
                sa.select(export.export_item.c.content_id).where(
                    export.export_item.c.id == carrier_id
                )
            ).scalar()
        return None if found is None else str(found)
