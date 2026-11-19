"""A KEPUB of an exported volume, for a Kobo (M5 L20b, EF-BIB-015, OPDS and `/lite`).

The KEPUB is made from the neutralized EPUB of a content (the file `ContentAccess` opens, never an
original: I35 extended) and kept in the cache of the renditions. Nothing here is a capability:

- an offer and a download are an `export_item` that has a live export record and whose content is
  an EPUB (as for OPDS-PSE: a volume that was exported, nothing else);
- at every call the profile is judged again by the one filter of every surface, on the Edition
  **and** on the content the item resolves to (`ContentAccess.resolve`), and the whole file is for
  the profile only when it does not read it where it was sent (ADR-0055 D-P1: a KEPUB is a full
  copy out, so a profile that reads the volume elsewhere is not offered one);
- every refusal is one exception, `KepubNotFoundError`: an item that is hidden, absent, not an
  EPUB, not exported, read elsewhere, without a readable copy or that cannot be converted are
  indistinguishable. A busy cache is said only after the profile is judged;
- the cache is asked **after** the thread slot is taken, so that a refusal for lack of a slot tells
  nothing of what another profile read (observation C1 of the review of OPDS-PSE);
- nothing is written for the reader: no position, no history, no mark (ENF-SEC-013).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Final, Protocol

import sqlalchemy as sa

from shelfhold.infrastructure.export_states import LIVE_STATES
from shelfhold.infrastructure.schema import catalog, export, storage
from shelfhold.library.application.kepub import KepubError
from shelfhold.library.application.renditions import UnknownSourceError
from shelfhold.library.domain.failures import TombstonedContentError
from shelfhold.reading.application.content_access import ContentAccess, NoReadableCopyError
from shelfhold.reading.application.progress import UnknownCarrierError

__all__ = [
    "MAX_EPUB_BYTES",
    "MAX_OFFERS",
    "KepubDelivery",
    "KepubFile",
    "KepubMaker",
    "KepubNotFoundError",
    "KepubOffer",
]

MAX_OFFERS: Final = 50  # the KEPUB links of one feed or page, so that it stays bounded
_MAX_SCAN: Final = 4 * MAX_OFFERS  # rows looked at to find them (a hidden one is skipped)
MAX_EPUB_BYTES: Final = 64 * 1024 * 1024  # what is read of a file to make a KEPUB of it
_FORMAT: Final = "epub"


class KepubNotFoundError(LookupError):
    """The one answer of every refusal."""

    def __init__(self) -> None:
        super().__init__("kepub not found")


class KepubMaker(Protocol):
    """The part of `RenditionService` a delivery needs."""

    def slot(self, owner: str) -> AbstractContextManager[None]: ...

    def kepub_bytes(self, content_id: str, data: bytes) -> bytes: ...


@dataclass(frozen=True)
class KepubOffer:
    item_id: str
    edition_id: str
    number: str  # the number of the Unit as it is written, for the label of the link


@dataclass(frozen=True)
class KepubFile:
    data: bytes
    title: str  # the title of the Work, read once the profile is judged: the name of the file


class KepubDelivery:
    def __init__(self, engine: sa.Engine, access: ContentAccess, renditions: KepubMaker) -> None:
        self._engine, self._access, self._renditions = engine, access, renditions

    # -- who may be given a KEPUB: the same judgement for an offer and for a download --------------

    def _live(self, connection: sa.Connection, item_id: str) -> bool:
        record = export.export_record
        return (
            connection.execute(
                sa.select(sa.literal(1))
                .where(record.c.export_item_id == item_id, record.c.state.in_(sorted(LIVE_STATES)))
                .limit(1)
            ).first()
            is not None
        )

    def _judge(self, connection: sa.Connection, user_id: str, item_id: str) -> str:
        """The content of the item, or `KepubNotFoundError`."""
        try:
            carrier = self._access.resolve_in(connection, user_id, "export_item", item_id)
            whole = self._access.resolve_in(connection, user_id, "content", carrier.content_id)
        except UnknownCarrierError:
            raise KepubNotFoundError from None
        if (
            carrier.format != _FORMAT
            or not whole.full_allowed
            or not self._live(connection, item_id)
        ):
            raise KepubNotFoundError
        return carrier.content_id

    # -- the offers of a feed or a page ----------------------------------------------------------

    def offers(self, user_id: str, edition_ids: list[str]) -> list[KepubOffer]:
        """The KEPUB the profile may be given among the exported EPUB volumes of these Editions
        (already the ones it may see), at most `MAX_OFFERS`; one that is not hers is skipped, never
        counted. The archive is not opened: what is offered is a link."""
        if not edition_ids:
            return []
        item, content, unit = export.export_item, storage.content, catalog.unit
        found: list[KepubOffer] = []
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(item.c.id, item.c.edition_id, unit.c.number_raw)
                .select_from(
                    item.join(content, content.c.id == item.c.content_id).join(
                        unit, unit.c.id == content.c.unit_id
                    )
                )
                .where(item.c.edition_id.in_(edition_ids), content.c.format == _FORMAT)
                .order_by(item.c.id)
                .limit(_MAX_SCAN)
            ).all()
            for row in rows:
                if len(found) >= MAX_OFFERS:
                    break
                try:
                    self._judge(connection, user_id, str(row.id))
                except KepubNotFoundError:
                    continue
                found.append(KepubOffer(str(row.id), str(row.edition_id), str(row.number_raw)))
        return found

    # -- the download ----------------------------------------------------------------------------

    def fetch(self, user_id: str, item_id: str) -> KepubFile:
        """The KEPUB of the item. `KepubNotFoundError` for every refusal; `RenditionBusyError` (the
        caller says 429) once the profile is judged and no thread is free."""
        with self._engine.connect() as connection:
            content_id = self._judge(connection, user_id, item_id)
            title = connection.execute(
                sa.select(catalog.work.c.title)
                .select_from(
                    export.export_item.join(
                        catalog.edition, catalog.edition.c.id == export.export_item.c.edition_id
                    ).join(catalog.work, catalog.work.c.id == catalog.edition.c.work_id)
                )
                .where(export.export_item.c.id == item_id)
            ).scalar_one_or_none()
        with self._renditions.slot(user_id):  # before the cache is looked in: no oracle on it
            try:
                archive = self._access.archives.open("content", content_id, "full")
                try:
                    raw = archive.whole(MAX_EPUB_BYTES)
                finally:
                    archive.close()
            except (NoReadableCopyError, ValueError, OSError):  # the copy, not the cache
                raise KepubNotFoundError from None
            try:
                data = self._renditions.kepub_bytes(content_id, raw)
            except (KepubError, UnknownSourceError, TombstonedContentError):
                raise KepubNotFoundError from None
            with self._engine.connect() as connection:  # the conversion took time: judged again
                self._judge(connection, user_id, item_id)
        return KepubFile(data, str(title or ""))
