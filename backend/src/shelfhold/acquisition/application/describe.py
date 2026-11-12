"""What a source says of a book, written into the catalog when its Work and Edition are made
(EF-SRC-007, EF-BIB-049, EF-SRC-024).

- the source's own identifiers become external ids of the Work (`external_id(gutenberg)`);
- the source's key for "the same work" groups its books under one Work, so that two published
  translations of one novel are two Editions of it and not two Works;
- authors are credited on the Work, with the death year the source gives (the calculation of
  `catalog.domain.public_domain` needs it); a translator is the credit of one Edition and is its
  text version (EF-BIB-049: a published translation is told apart by its translator);
- the rights basis the manifest declares is stored with the jurisdiction it holds in. "Public
  domain in the US" is a claim about the United States and stays one: nothing here turns it into
  a statement about the country of the instance.

Every text is made safe by the caller's `clean` before it is stored (ENF-SEC-015).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import sqlalchemy as sa
import structlog

from shelfhold.catalog.public import (
    Catalog,
    ExternalId,
    ExternalIdError,
    TextVersionRef,
    holder_of,
    propose_fields,
)
from shelfhold.infrastructure.schema import catalog
from shelfhold.sdk.metadata import FieldProposal, Provenance
from shelfhold.sdk.refs import SeriesInfo

__all__ = [
    "credit_authors",
    "incoming_work_ids",
    "known_work",
    "text_version_for",
    "write_rights",
]

log = structlog.get_logger(__name__)
WRITER = "aut"
TRANSLATOR = "trl"


def _provider(key: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in key.casefold()) + "_work"


def known_work(connection: sa.Connection, plugin_key: str, info: SeriesInfo) -> str | None:
    """The Work an earlier book of this source with the same `work_key` was filed under."""
    if not info.work_key:
        return None
    try:
        return holder_of(connection, ExternalId("work", _provider(plugin_key), info.work_key))
    except ExternalIdError:
        return None


def incoming_work_ids(plugin_key: str, info: SeriesInfo) -> tuple[ExternalId, ...]:
    """The source's own identifiers of its Work, and the key that groups its books: what the
    arrival of the Edition hands over (`AutoAttachService.arrive`), which asks the Work tombstones
    before any of them is recorded."""
    pairs = [(_provider(plugin_key), info.work_key), *info.external_ids.items()]
    return tuple(ExternalId("work", provider, value) for provider, value in pairs if value)


def credit_authors(
    connection: sa.Connection, work_id: str, info: SeriesInfo, clean: Callable[[str], str | None]
) -> None:
    books = Catalog(connection)
    names = [c for c in map(clean, info.authors) if c]
    if names:
        books.credit_work(work_id, WRITER, names)
    for name, year in info.author_death_years.items():
        cleaned = clean(name)
        person = books.contributor(cleaned) if cleaned else None
        if person is not None and 0 < year < 3000:
            books.set_death_year(person, year)


def text_version_for(
    connection: sa.Connection, info: SeriesInfo, clean: Callable[[str], str | None]
) -> tuple[TextVersionRef | None, str | None]:
    """The text version of a published translation: its (first) translator, and who that is."""
    for name in info.translators:
        cleaned = clean(name)
        person = Catalog(connection).contributor(cleaned) if cleaned else None
        if person is not None:
            return TextVersionRef(contributor_id=person), person
    return None, None


def write_rights(
    connection: sa.Connection,
    edition_id: str,
    *,
    basis: str | None,
    jurisdiction: str | None,
    attribution: str | None = None,
    plugin_key: str = "",
    now: datetime | None = None,
) -> None:
    """What the manifest declares, once, on a new Edition: never rewritten after.

    The attribution a source gives (`SeriesInfo.rights`) is written the same way: into
    `edition.rights` when it is empty, with its provenance (the source that said it), so that the
    export can copy it into the file and the card can show it (EF-BIB-033)."""
    table = catalog.edition
    if basis is not None or jurisdiction is not None:
        connection.execute(
            sa.update(table)
            .where(table.c.id == edition_id, table.c.rights_basis.is_(None))
            .values(rights_basis=basis, rights_jurisdiction=jurisdiction)
        )
    if not attribution:
        return
    written = connection.execute(
        sa.update(table)
        .where(table.c.id == edition_id, table.c.rights.is_(None))
        .values(rights=attribution)
    )
    if written.rowcount and now is not None:
        propose_fields(
            connection,
            {"edition": edition_id},
            [
                FieldProposal(
                    entity_type="edition",
                    field="rights",
                    value=attribution,
                    confidence=1.0,
                    provenance=Provenance(provider=plugin_key, fetched_at=now),
                )
            ],
            now=now,
            origin="source",
        )
