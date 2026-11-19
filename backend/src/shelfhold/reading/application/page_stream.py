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

Which scope (ADR-0055 D-P1, unchanged). A stream is offered only for an item that has a live export
record, and what is streamed is judged on the content it holds: the whole kept copy when the profile
does not read it where it was sent (no live, verified account of her own on a destination that holds
it), else an excerpt, cut by the server with the very bounds of `/content`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import sqlalchemy as sa

from shelfhold.infrastructure.export_states import LIVE_STATES
from shelfhold.infrastructure.schema import catalog, export, storage
from shelfhold.reading.application.content_access import (
    ContentAccess,
    ContentArchive,
    NoReadableCopyError,
)
from shelfhold.reading.application.progress import UnknownCarrierError
from shelfhold.reading.domain.content_ticket import Scope
from shelfhold.reading.domain.content_types import media_type_of
from shelfhold.reading.domain.publication import page_names

__all__ = ["MAX_STREAMS", "PageStream", "StreamBudget", "StreamOffer", "StreamPage"]

MAX_STREAMS: Final = 50  # the streams one feed offers in all, so a feed stays bounded
_MAX_SCAN: Final = 4 * MAX_STREAMS  # rows looked at to find them (a hidden one is skipped)
_CARRIER: Final = "export_item"
_FORMAT: Final = "cbz"


@dataclass
class StreamBudget:
    """What one feed may spend, across all its Editions: streams offered and archives opened (an
    archive opened and left out costs as much as one that is offered)."""

    offers: int = MAX_STREAMS
    opens: int = MAX_STREAMS


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

    def offers(
        self, user_id: str, edition_id: str, budget: StreamBudget | None = None
    ) -> list[StreamOffer]:
        """The streams of an Edition the caller has already shown to the profile: those whose item
        and content the profile sees now, whose kept copy can be read and has pages."""
        budget = budget or StreamBudget()
        item, content, unit = export.export_item, storage.content, catalog.unit
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(item.c.id, unit.c.number_raw)
                .select_from(
                    item.join(content, content.c.id == item.c.content_id).outerjoin(
                        unit, unit.c.id == content.c.unit_id
                    )
                )
                .where(
                    item.c.edition_id == edition_id,
                    content.c.format == _FORMAT,
                    sa.exists().where(  # an item that was never exported is no stream
                        export.export_record.c.export_item_id == item.c.id,
                        export.export_record.c.state.in_(sorted(LIVE_STATES)),
                    ),
                )
                .order_by(item.c.id)
                .limit(_MAX_SCAN)
            ).all()
        found: list[StreamOffer] = []
        for row in rows:
            if budget.offers <= 0 or budget.opens <= 0:
                break
            budget.opens -= 1
            item_id = str(row.id)
            try:
                count = self._count(user_id, item_id)
            except UnknownCarrierError:
                continue  # what the profile may not see is not offered, and is not named
            budget.offers -= 1
            found.append(StreamOffer(item_id, count, row.number_raw))
        return found

    def _count(self, user_id: str, item_id: str) -> int:
        content_id, scope = self._judge(user_id, item_id)
        archive = self._open(content_id, scope)
        try:
            count = len(page_names(archive.names()))
        finally:
            archive.close()
        if count == 0:
            raise UnknownCarrierError
        return count

    def page(self, user_id: str, item_id: str, number: int) -> StreamPage:
        """Page `number` (from 0) of the volume. `UnknownCarrierError` for every refusal."""
        content_id, scope = self._judge(user_id, item_id)
        archive = self._open(content_id, scope)
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

    def _judge(self, user_id: str, item_id: str) -> tuple[str, Scope]:
        """The content to read and how much of it, judged now: the item and its content are seen by
        the profile (the one filter), the item is live somewhere, and the scope is the content's
        own (`full_allowed`), never the item's."""
        resolved = self._access.resolve(user_id, _CARRIER, item_id)
        if resolved.format != _FORMAT or not self._live(item_id):
            raise UnknownCarrierError
        own = self._access.resolve(user_id, "content", resolved.content_id)  # visible, and scope
        return resolved.content_id, "full" if own.full_allowed else "excerpt"

    def _live(self, item_id: str) -> bool:
        record = export.export_record
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(sa.literal(1))
                    .where(
                        record.c.export_item_id == item_id, record.c.state.in_(sorted(LIVE_STATES))
                    )
                    .limit(1)
                ).first()
                is not None
            )

    def _open(self, content_id: str, scope: Scope) -> ContentArchive:
        """The file of the content that was judged, never of what the item names at this moment
        (X4: the bytes read are those of the `content_id` the filter passed). For an excerpt the
        archive shows only the cut the server made (`ExcerptUnavailableError`: nothing to cut)."""
        try:
            return self._access.archives.open("content", content_id, scope)
        except NoReadableCopyError:
            raise UnknownCarrierError from None
