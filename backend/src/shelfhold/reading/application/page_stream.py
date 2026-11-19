"""The pages of an exported volume, one by one, for an e-reader (M5 L18, EF-INT-007, OPDS-PSE).

A stream is an `export_item` whose content is a CBZ: the page `n` (from 0, as the PSE
specification counts) is the `n`-th image of its reading order, read from the kept copy the port of
`ContentAccess` opens (never an original: I35 extended). Nothing here is a capability:

- at every call the profile is judged again by the one filter of every surface, on the Edition
  **and** on the content the item resolves to (`ContentAccess.resolve`: an `export_item` names both
  and nothing ties them);
- every refusal is `UnknownCarrierError`, one answer: an item that is hidden, absent, not a CBZ,
  without a readable copy, or a page that is not there are indistinguishable;
- a name is only ever taken from the archive the server chose, never from the client;
- nothing is written: no position, no history, no mark. Where an e-reader is, is kosync's.

Which scope. A device token reads for the profile's own e-reader the file that was exported for it
(the same file an OPDS download hands over), so the whole volume is streamed; the reader of the web
(`/content`) keeps the excerpt rule of ADR-0055 D-P1 for the same file. Choice to validate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import sqlalchemy as sa

from shelfhold.infrastructure.schema import catalog, export, storage
from shelfhold.reading.application.content_access import (
    ContentAccess,
    ContentArchive,
    NoReadableCopyError,
)
from shelfhold.reading.application.progress import UnknownCarrierError
from shelfhold.reading.domain.content_types import media_type_of
from shelfhold.reading.domain.publication import page_names

__all__ = ["MAX_STREAMS", "PageStream", "StreamOffer", "StreamPage"]

MAX_STREAMS: Final = 50  # the streams one Edition's entry of a feed offers, so a feed stays bounded
_CARRIER: Final = "export_item"
_FORMAT: Final = "cbz"


@dataclass(frozen=True)
class StreamOffer:
    item_id: str
    count: int  # the number of pages
    number: str | None  # the number of the Unit, as it was written


@dataclass(frozen=True)
class StreamPage:
    content_id: str  # the content the page was read from (what a rendition is keyed to)
    name: str
    media_type: str
    body: bytes


class PageStream:
    def __init__(self, engine: sa.Engine, access: ContentAccess) -> None:
        self._engine, self._access = engine, access

    def offers(self, user_id: str, edition_id: str) -> list[StreamOffer]:
        """The streams of an Edition the caller has already shown to the profile: those whose item
        and content the profile sees now, whose kept copy can be read and has pages."""
        item, content, unit = export.export_item, storage.content, catalog.unit
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(item.c.id, unit.c.number_raw)
                .select_from(
                    item.join(content, content.c.id == item.c.content_id).outerjoin(
                        unit, unit.c.id == content.c.unit_id
                    )
                )
                .where(item.c.edition_id == edition_id, content.c.format == _FORMAT)
                .order_by(item.c.id)
                .limit(MAX_STREAMS)
            ).all()
        found: list[StreamOffer] = []
        for row in rows:
            item_id = str(row.id)
            try:
                count = self._count(user_id, item_id)
            except UnknownCarrierError:
                continue  # what the profile may not see is not offered, and is not named
            found.append(StreamOffer(item_id, count, row.number_raw))
        return found

    def _count(self, user_id: str, item_id: str) -> int:
        self._resolve(user_id, item_id)
        archive = self._open(item_id)
        try:
            count = len(page_names(archive.names()))
        finally:
            archive.close()
        if count == 0:
            raise UnknownCarrierError
        return count

    def page(self, user_id: str, item_id: str, number: int) -> StreamPage:
        """Page `number` (from 0) of the volume. `UnknownCarrierError` for every refusal."""
        content_id = self._resolve(user_id, item_id)
        archive = self._open(item_id)
        try:
            names = page_names(archive.names())
            if not 0 <= number < len(names):
                raise UnknownCarrierError
            name = names[number]
            body = archive.read(name)
        except (KeyError, OSError, ValueError):
            raise UnknownCarrierError from None
        finally:
            archive.close()
        return StreamPage(content_id, name, media_type_of(name), body)

    def _resolve(self, user_id: str, item_id: str) -> str:
        resolved = self._access.resolve(user_id, _CARRIER, item_id)  # the filter, now
        if resolved.format != _FORMAT:
            raise UnknownCarrierError
        return resolved.content_id

    def _open(self, item_id: str) -> ContentArchive:
        try:
            return self._access.archives.open(_CARRIER, item_id, "full")
        except NoReadableCopyError:
            raise UnknownCarrierError from None
