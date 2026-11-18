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

from collections.abc import Callable
from contextlib import ExitStack
from typing import Any

import sqlalchemy as sa

from shelfhold.infrastructure.archives import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.fsops import Fsops, FsopsError
from shelfhold.infrastructure.schema import export, storage
from shelfhold.library.adapters.epub_derivative import servable_epub
from shelfhold.library.domain.failures import OriginalNotServableError
from shelfhold.library.replica import library_copies
from shelfhold.reading.application.content_access import NoReadableCopyError

__all__ = ["ReadableCopies"]


class _Open:
    def __init__(self, stack: ExitStack, archive: Any) -> None:
        self._stack, self._archive = stack, archive

    def names(self) -> frozenset[str]:
        return frozenset(e.name for e in self._archive.entries if not e.is_dir)

    def read(self, name: str) -> bytes:
        return bytes(self._archive.read(name))

    def close(self) -> None:
        self._stack.close()


class ReadableCopies:
    """The `ArchiveSource` of `reading.application.content_access`, over the library."""

    def __init__(self, engine: sa.Engine, fsops: Callable[[], Fsops]) -> None:
        self._engine, self._fsops = engine, fsops

    def open(self, carrier_type: str, carrier_id: str) -> _Open:
        content_id = self._content_id(carrier_type, carrier_id)
        if content_id is None:
            raise NoReadableCopyError
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    storage.content.c.id, storage.content.c.unit_id, storage.content.c.format
                ).where(storage.content.c.id == content_id)
            ).first()
            if row is None or row.unit_id is None:
                raise NoReadableCopyError
            copies = (
                []
                if row.format != "cbz"
                else [c for c in library_copies(connection, row.unit_id) if not c.damaged]
            )
        fsops = self._fsops()
        if row.format == "epub":
            try:
                served = servable_epub(self._engine, fsops, row.unit_id)
            except OriginalNotServableError:
                raise NoReadableCopyError from None
            if served.content_id != row.id:
                raise NoReadableCopyError
            paths = [served.path]
        elif row.format == "cbz":
            paths = [c.local for c in copies]
        else:
            raise NoReadableCopyError
        for path in paths:
            stack = ExitStack()
            try:
                stream = stack.enter_context(fsops.open_read(path))
                archive = stack.enter_context(safe_open_archive(stream, name=path))
            except (OSError, FsopsError, UnsafeArchiveError):
                stack.close()
                continue
            return _Open(stack, archive)
        raise NoReadableCopyError

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
