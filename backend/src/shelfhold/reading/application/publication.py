"""The manifest of a publication: what the reader opens (M5 L06, EF-LEC-014, EF-LEC-005, ADR-0055).

`GET /api/reader/{carrier_type}/{carrier_id}/publication` answers, for a profile and the session
that asks: the title, the direction of reading, the pages of an image carrier or the reading order
of an EPUB, and for each entry a link into `/content` carrying a ticket. Nothing here reads a byte
of a book except through the port of `ContentAccess`, which gives the **neutralized derivative** of
an `untrusted` EPUB and never the original (I35 extended), and for an excerpt only the closed list
the server cut. The manifest is built from that archive and from nothing a caller supplies: the
names are the archive's own.

Which carrier (ADR-0055 D-P1). A carrier is named by the caller as a `content` (the kept copy), an
`export_item` (the file that was exported) or a `unit`, in which case the server chooses:
- the kept copy, read in full, when the profile does not read the exported file where it was sent
  (a Work that is not exported, or exported where she has no account);
- else the exported file (`export_item`), as an excerpt: the exported content is read where it was
  sent, Shelfhold shows a sample only;
- else the kept copy as an excerpt. No file at all is `NoReadableCopyError` (409 with an action).

A manifest writes nothing: no position, no history, no state; an excerpt never counts for
« Continuer la lecture » and never travels to a reading server.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

import sqlalchemy as sa
import structlog

from shelfhold.catalog.public import visible_edition_ids
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.schema import catalog, export, storage
from shelfhold.infrastructure.xml import UnsafeXmlError, safe_parse_xml
from shelfhold.reading.application.content_access import (
    ContentAccess,
    ContentArchive,
    FullReadNotAllowedError,
    NoReadableCopyError,
    ResolvedCarrier,
)
from shelfhold.reading.application.progress import UnknownCarrierError
from shelfhold.reading.domain.content_ticket import Scope
from shelfhold.reading.domain.content_types import media_type_of, safe_entry
from shelfhold.reading.domain.publication import (
    ReadingDirection,
    page_names,
    reading_direction,
    resolve_in_package,
)

log = structlog.get_logger(__name__)

__all__ = [
    "CARRIER_TYPES",
    "Entry",
    "Listing",
    "Publication",
    "PublicationService",
    "epub_spine_of",
    "list_archive",
]

CARRIER_TYPES: Final = ("content", "export_item", "unit")
_CONTAINER: Final = "META-INF/container.xml"
_READ_FORMATS: Final = frozenset({"cbz", "epub"})
_MAX_SPINE: Final = 20_000
_EXPORTED: Final = "export_item"


@dataclass(frozen=True)
class Listing:
    """The entries of an archive, in reading order, as the archive names them."""

    kind: Literal["pages", "epub"]
    pages: tuple[str, ...] = ()
    spine: tuple[str, ...] = ()
    package: str | None = None
    language: str | None = None
    direction: str | None = None


@dataclass(frozen=True)
class Entry:
    index: int
    href: str  # a link into `/content`, ticket included
    media_type: str


@dataclass(frozen=True)
class Publication:
    carrier_type: str
    carrier_id: str
    series_id: str  # the Œuvre: the anchor of a setting pinned to this series (L13)
    edition_id: str  # the Édition it is a copy of: what `?since=` names when it leaves her view
    scope: Scope
    kind: Literal["pages", "epub"]
    title: str
    number: str | None
    language: str | None
    reading_direction: ReadingDirection
    expires_at: datetime
    pages: tuple[Entry, ...]
    spine: tuple[Entry, ...]
    package: str | None


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _epub_listing(archive: ContentArchive) -> Listing:
    names = archive.names()
    if _CONTAINER not in names:
        raise NoReadableCopyError
    try:
        container = safe_parse_xml(archive.read(_CONTAINER))
        package = next(
            (
                str(e.get("full-path"))
                for e in container.iter()
                if _local(e.tag) == "rootfile" and e.get("full-path")
            ),
            None,
        )
        if package is None or package not in names or posixpath.isabs(package):
            raise NoReadableCopyError
        root = safe_parse_xml(archive.read(package))
    except (UnsafeXmlError, KeyError, OSError, ValueError):
        raise NoReadableCopyError from None
    manifest: dict[str, str] = {}
    language: str | None = None
    direction: str | None = None
    refs: list[tuple[str, bool]] = []
    for element in root.iter():
        local = _local(element.tag)
        if local == "item" and element.get("id") and element.get("href"):
            manifest[str(element.get("id"))] = str(element.get("href"))
        elif local == "language" and language is None and (element.text or "").strip():
            language = str(element.text).strip()[:35]
        elif local == "spine":
            direction = element.get("page-progression-direction")
        elif local == "itemref" and element.get("idref"):
            refs.append((str(element.get("idref")), element.get("linear", "yes") != "no"))
    documents: list[str] = []
    for ref, linear in refs[:_MAX_SPINE]:
        entry = resolve_in_package(package, manifest.get(ref, ""), names)
        if (
            entry is not None
            and linear
            and safe_entry(entry)
            and media_type_of(entry) == "application/xhtml+xml"
        ):
            documents.append(entry)
    if not documents:
        raise NoReadableCopyError  # a package without a reading order is no book to open
    return Listing(
        "epub", spine=tuple(documents), package=package, language=language, direction=direction
    )


def list_archive(archive: ContentArchive, fmt: str) -> Listing:
    """The reading order of an archive the port opened: its pages (`cbz`) or its spine (`epub`).
    For an excerpt the archive shows only the cut, so the list is the cut."""
    if fmt == "epub":
        return _epub_listing(archive)
    if fmt == "cbz":
        pages = page_names(archive.names())
        if not pages:
            raise NoReadableCopyError
        return Listing("pages", pages=tuple(pages))
    raise NoReadableCopyError  # a format that is not read here (the PDF engine is not in M5)


def epub_spine_of(access: Callable[[], ContentAccess]) -> Callable[[str], int | None]:
    """How many documents the spine of an exported EPUB has, for kosync (M5 L09): `None` when the
    file
    cannot be opened or has no reading order. Asked only for a position that may be the end of a
    book; the file is the one `/content` would serve (the neutralized derivative), opened and closed
    here, and nothing of it is kept."""

    def spine_of(item_id: str) -> int | None:
        try:
            archive = access().archives.open(_EXPORTED, item_id, "full")
        except Exception as error:  # a PUT of a device is never a 500: nothing is read
            log.info("kosync.spine_unavailable", reason=type(error).__name__)
            return None
        try:
            return len(_epub_listing(archive).spine)
        except Exception as error:
            log.info("kosync.spine_unavailable", reason=type(error).__name__)
            return None
        finally:
            try:
                archive.close()
            except Exception:  # a device's PUT is never a 500
                log.info("kosync.spine_unavailable", reason="close")

    return spine_of


class PublicationService:
    def __init__(self, engine: sa.Engine, access: ContentAccess) -> None:
        self._engine, self._access = engine, access

    # -- which carrier -----------------------------------------------------------------------

    def choose(self, user_id: str, carrier_type: str, carrier_id: str) -> tuple[str, str]:
        """The carrier to read: the one named, or for a `unit` the one ADR-0055 D-P1 picks.
        `UnknownCarrierError` for a type that does not exist, a thing absent or hidden."""
        if carrier_type in {"content", "export_item"}:
            return carrier_type, carrier_id
        if carrier_type != "unit":
            raise UnknownCarrierError
        with self._engine.connect() as connection:
            edition = connection.execute(
                sa.select(catalog.unit.c.edition_id).where(catalog.unit.c.id == carrier_id)
            ).scalar()
            if edition is None or str(edition) not in visible_edition_ids(
                connection, reader_scope(connection, user_id), [str(edition)]
            ):
                raise UnknownCarrierError
            contents = [
                str(c)
                for c in connection.execute(
                    sa.select(storage.content.c.id)
                    .where(storage.content.c.unit_id == carrier_id)
                    .order_by(storage.content.c.id)
                ).scalars()
            ]
            items = [
                str(i)
                for i in connection.execute(
                    sa.select(export.export_item.c.id)
                    .join(storage.content, storage.content.c.id == export.export_item.c.content_id)
                    .join(
                        export.unit_coverage,
                        sa.and_(
                            export.unit_coverage.c.carrier_type == "export_item",
                            export.unit_coverage.c.carrier_id == export.export_item.c.id,
                        ),
                    )
                    .where(
                        export.unit_coverage.c.unit_id == carrier_id,
                        storage.content.c.unit_id == carrier_id,  # a file of this Unit, no other
                    )
                    .order_by(export.export_item.c.id)
                ).scalars()
            ]
        seen = self._seen(user_id, [("content", c) for c in contents])
        for found in seen:
            if found[1].full_allowed:
                return found[0]  # the kept copy that is not read elsewhere: in full
        for item in self._seen(user_id, [("export_item", i) for i in items]):
            return item[0]  # read where it was sent: a sample here
        for found in seen:
            return found[0]
        raise NoReadableCopyError

    def _seen(
        self, user_id: str, carriers: list[tuple[str, str]]
    ) -> list[tuple[tuple[str, str], ResolvedCarrier]]:
        out: list[tuple[tuple[str, str], ResolvedCarrier]] = []
        for carrier in carriers:
            try:
                resolved = self._access.resolve(user_id, *carrier)
            except UnknownCarrierError:
                continue  # what the profile may not see is not a candidate, and is not named
            if resolved.format in _READ_FORMATS:
                out.append((carrier, resolved))
        return out

    # -- the manifest ------------------------------------------------------------------------

    def publication(
        self,
        *,
        user_id: str,
        session_id: str,
        carrier_type: str,
        carrier_id: str,
        scope: Scope | None = None,
    ) -> Publication:
        """Raises `UnknownCarrierError` (absent or hidden: one answer), `FullReadNotAllowedError`
        (a full read asked of what is an excerpt), `ExcerptUnavailableError` (nothing can be cut
        within the bound), `NoReadableCopyError` (no file that may be read)."""
        carrier_type, carrier_id = self.choose(user_id, carrier_type, carrier_id)
        resolved = self._access.resolve(user_id, carrier_type, carrier_id)
        if resolved.format not in _READ_FORMATS:
            raise NoReadableCopyError
        chosen: Scope = scope or ("full" if resolved.full_allowed else "excerpt")
        if chosen == "full" and not resolved.full_allowed:
            raise FullReadNotAllowedError  # before a file is opened or a ticket is made
        archive = self._access.archives.open(carrier_type, carrier_id, chosen)
        try:
            listing = list_archive(archive, resolved.format)
        finally:
            archive.close()
        issued = self._access.issue(
            user_id=user_id,
            session_id=session_id,
            carrier_type=carrier_type,
            carrier_id=carrier_id,
            scope=chosen,
        )

        def entry_of(index: int, name: str) -> Entry:
            return Entry(index, self._access.url(issued.token, name), media_type_of(name))

        title, number, kind_direction, work_direction, series, edition = self._describe(
            resolved.content_id
        )
        return Publication(
            carrier_type=carrier_type,
            carrier_id=carrier_id,
            series_id=series,
            edition_id=edition,
            scope=chosen,
            kind=listing.kind,
            title=title,
            number=number,
            language=listing.language,
            reading_direction=reading_direction(work_direction, listing.direction, kind_direction),
            expires_at=issued.expires_at,
            pages=tuple(entry_of(i, n) for i, n in enumerate(listing.pages, start=1)),
            spine=tuple(entry_of(i, n) for i, n in enumerate(listing.spine, start=1)),
            package=self._access.url(issued.token, listing.package) if listing.package else None,
        )

    def _describe(
        self, content_id: str
    ) -> tuple[str, str | None, str | None, str | None, str, str]:
        """The title and number of what is read, the two places a direction may be set, and the
        Œuvre it belongs to (the anchor of the settings of L13)."""
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    catalog.work.c.title,
                    catalog.unit.c.number_raw,
                    catalog.work.c.reading_direction,
                    catalog.work_kind.c.default_direction,
                    catalog.work.c.id,
                    catalog.edition.c.id.label("edition_id"),
                )
                .select_from(
                    storage.content.join(
                        catalog.unit, catalog.unit.c.id == storage.content.c.unit_id
                    )
                    .join(catalog.edition, catalog.edition.c.id == catalog.unit.c.edition_id)
                    .join(catalog.work, catalog.work.c.id == catalog.edition.c.work_id)
                    .join(catalog.work_kind, catalog.work_kind.c.key == catalog.work.c.kind)
                )
                .where(storage.content.c.id == content_id)
            ).first()
        if row is None:
            raise UnknownCarrierError
        return (
            str(row.title),
            row.number_raw,
            row.default_direction,
            row.reading_direction,
            str(row.id),
            str(row.edition_id),
        )
