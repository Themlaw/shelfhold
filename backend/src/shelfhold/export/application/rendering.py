"""The files written for a destination: a CBZ (below) and an EPUB (`EpubRenderer`, end of file).

The CBZ is the pages as received, with a ComicInfo made for it (EF-EXP-011).

The ComicInfo is built from the database (never from the upstream document), the pages are copied
from the audited received archive, and the payload is checked again on the way out: the rendered
archive must have the `content_hash` of the original, so that only the sidecar differs (G1).
"""

import hashlib
import re
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, BinaryIO

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection

from shelfhold.catalog.public import ContributorName, NameKind, effective_age, parse_partial_date
from shelfhold.infrastructure.archives import SafeArchive, safe_open_archive
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.packaging.cbz import repackage_cbz
from shelfhold.infrastructure.packaging.comicinfo import (
    ComicInfoData,
    ComicInfoPerson,
    render_comicinfo,
)
from shelfhold.infrastructure.packaging.epub import OpfEdit, repackage_epub
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.library.public import content_hash
from shelfhold.sdk.age import AgeRating

_ADULT_FROM = 18  # M1a has adult shelves only (a ceiling of 18 or more, or none)

__all__ = [
    "CbzRenderer",
    "EpubRenderer",
    "RenderedCbz",
    "RenderedEpub",
    "comicinfo_data",
    "comicinfo_sha256",
    "comicinfo_xml",
    "epub_opf_edit",
]

log = structlog.get_logger(__name__)

# MARC relator of a credit -> the ComicInfo person field it is written to (EF-BIB-046)
_ROLE_FIELDS = {"aut": "Writer", "art": "Penciller", "ill": "Penciller"}


@dataclass
class RenderedCbz:
    file: BinaryIO  # positioned at 0, closed by the caller
    size: int
    file_hash: str  # `sha256:` of the rendered archive
    content_hash: str  # of its pages: equal to the original's
    sidecar_sha256: str


class RenderMismatchError(Exception):
    """The pages of the rendered archive are not those of the original."""


def _adopted_series(connection: Connection, unit_id: str) -> Any:
    """The values frozen when the series was adopted: a new file inherits exactly these
    (`Series` aside, which the caller gives), never the titles of the source (EF-EXP-052)."""
    unit, link, series = catalog.unit, export.edition_export_series, export.export_series
    return connection.execute(
        sa.select(
            series.c.frozen_series_sort, series.c.frozen_localized_series, series.c.frozen_language
        )
        .select_from(
            unit.join(link, link.c.edition_id == unit.c.edition_id).join(
                series, series.c.id == link.c.export_series_id
            )
        )
        .where(unit.c.id == unit_id, series.c.frozen_at.is_not(None))
        .limit(1)
    ).first()


def _rating(age_rating: int | None, age_x: bool) -> AgeRating | None:
    if age_x:
        return AgeRating(18, True)
    return None if age_rating is None else AgeRating(int(age_rating))


def _unrated_age(connection: Connection, row: Any) -> AgeRating | None:
    """The `AgeRating` of a Work nobody has rated (a series found in the library, EF-EXP-011).

    A file never leaves without one: on an adult shelf (every shelf of M1a, and the safe side when
    no shelf is known) it is `Adults Only 18+`, or `X18+` when the source flagged the sub-source
    as adult-only (`isNsfw`) or an adopted ComicInfo said `X18+`: an X age suggestion, whatever its
    origin, so it is never written lower than what the sources said. The Work stays unrated in the
    database (invisible to a child profile, EF-USR-015); this is only what the file says. An
    explicit shelf below 18+ keeps the value empty: nothing unrated is exported there.
    """
    ceilings = _shelf_ceilings(connection, row.edition_id, row.shelf_id)
    if any(c is not None and c < _ADULT_FROM for c in ceilings):
        return None
    suggestion = catalog.age_suggestion
    flagged = connection.execute(
        sa.select(suggestion.c.id)
        .where(
            sa.or_(
                sa.and_(
                    suggestion.c.entity_type == "edition", suggestion.c.entity_id == row.edition_id
                ),
                sa.and_(suggestion.c.entity_type == "work", suggestion.c.entity_id == row.work_id),
            ),
            suggestion.c.x,
            suggestion.c.state != "rejected",
        )
        .limit(1)
    ).first()
    return AgeRating(_ADULT_FROM, x=flagged is not None)


def _shelf_ceilings(
    connection: Connection, edition_id: str, shelf_id: str | None
) -> list[int | None]:
    """`max_age_rating` of every shelf this Edition is filed on or exported to."""
    shelf, destination = export.shelf, export.shelf_destination
    link, series = export.edition_export_series, export.export_series
    rows = connection.execute(
        sa.select(shelf.c.max_age_rating)
        .select_from(
            link.join(series, series.c.id == link.c.export_series_id)
            .join(destination, destination.c.id == series.c.shelf_destination_id)
            .join(shelf, shelf.c.id == destination.c.shelf_id)
        )
        .where(link.c.edition_id == edition_id)
    ).scalars()
    found: list[int | None] = list(rows)
    if shelf_id is not None:
        found.append(
            connection.execute(
                sa.select(shelf.c.max_age_rating).where(shelf.c.id == shelf_id)
            ).scalar()
        )
    return found


def _people(connection: Connection, work_id: str) -> dict[str, list[ComicInfoPerson]]:
    """The people credited on the Work, per ComicInfo field, in credit order. Every known name
    goes to the writer, which picks the display form and keeps commas out (I21)."""
    credit, contributor, name = catalog.contribution, catalog.contributor, catalog.contributor_name
    rows = connection.execute(
        sa.select(
            credit.c.role, credit.c.contributor_id, contributor.c.kind, contributor.c.primary_name
        )
        .select_from(credit.join(contributor, contributor.c.id == credit.c.contributor_id))
        .where(credit.c.entity_type == "work", credit.c.entity_id == work_id)
        .order_by(credit.c.position, credit.c.contributor_id)
    ).all()
    names: dict[str, list[ContributorName]] = {}
    for found in connection.execute(
        sa.select(name).where(name.c.contributor_id.in_({r.contributor_id for r in rows}))
    ):
        names.setdefault(found.contributor_id, []).append(
            ContributorName(
                found.name, found.lang, found.script or "", NameKind(found.kind),
                bool(found.family_first),
            )
        )  # fmt: skip
    people: dict[str, list[ComicInfoPerson]] = {}
    for row in rows:
        field = _ROLE_FIELDS.get(row.role)
        if field is None:
            continue
        known = tuple(names.get(row.contributor_id) or [
            ContributorName(row.primary_name, "und", "", NameKind.DISPLAY)
        ])  # fmt: skip
        people.setdefault(field, []).append(ComicInfoPerson(known, is_org=row.kind == "org"))
    return people


def _dated(published_on: str | None) -> tuple[int | None, int | None, int | None]:
    """`Year`/`Month`/`Day`: never finer than the stored date (EF-BIB-047)."""
    date = parse_partial_date(published_on)
    return (date.year, date.month, date.day) if date else (None, None, None)


_URL = re.compile(r"https?://[^\s<>\"']+")


def _rights(connection: Connection, edition_id: str) -> tuple[str, str]:
    """`(Notes, Web)` for an Edition that has `rights` (a licence or a text, with its origin URL):
    copied into the file because the file itself says nothing of them (EF-BIB-033). An Edition
    without rights writes neither, and the file carries no mention of Shelfhold."""
    text = connection.execute(
        sa.select(catalog.edition.c.rights).where(catalog.edition.c.id == edition_id)
    ).scalar()
    if not text or not text.strip():
        return "", ""
    found = _URL.search(text)
    return text.strip(), found.group(0).rstrip(".,;)") if found else ""


_LICENCE = re.compile(r"\bCC[ -]BY(?:[ -][A-Z]{2})*\b")


def licence_of(rights: str) -> str | None:
    """The Creative Commons licence a rights text names, normalised for a comparison (`ccbysa`),
    or None. A text under such a licence asks to be attributed wherever the file goes."""
    found = _LICENCE.search(rights or "")
    return re.sub(r"[^a-z0-9]", "", found[0].casefold()) if found else None


def comicinfo_data(connection: Connection, unit_id: str, series: str) -> ComicInfoData:
    unit, edition, work, team = catalog.unit, catalog.edition, catalog.work, catalog.team
    variant = catalog.unit_variant
    row = connection.execute(
        sa.select(
            unit.c.title, unit.c.number_norm, unit.c.track, edition.c.lang, edition.c.publisher,
            work.c.sort_title, work.c.reading_direction, work.c.age_rating, work.c.age_x,
            edition.c.age_rating.label("edition_age_rating"),
            edition.c.age_x.label("edition_age_x"),
            team.c.label, edition.c.id.label("edition_id"), edition.c.work_id,
            edition.c.shelf_id, unit.c.published_on, unit.c.date_precision,
        )
        .select_from(
            unit.join(edition, edition.c.id == unit.c.edition_id)
            .join(work, work.c.id == edition.c.work_id)
            .outerjoin(variant, sa.and_(variant.c.unit_id == unit.c.id, variant.c.preferred))
            .outerjoin(team, team.c.id == variant.c.team_id)
        )
        .where(unit.c.id == unit_id)
    ).one()  # fmt: skip
    adopted = _adopted_series(connection, unit_id)
    # the most restrictive of the Work and its Edition (EF-BIB-037), X above every age
    age = effective_age(
        _rating(row.age_rating, row.age_x), [_rating(row.edition_age_rating, row.edition_age_x)]
    )
    if age is None:
        age = _unrated_age(connection, row)
    year, month, day = _dated(row.published_on)
    notes, web = _rights(connection, row.edition_id)
    return ComicInfoData(
        series=series,
        title=row.title or "",
        number=row.number_norm if row.track == "main" else "",
        series_sort=(adopted.frozen_series_sort if adopted else None) or row.sort_title or "",
        localized_series=(adopted.frozen_localized_series if adopted else None) or "",
        language_iso=(adopted.frozen_language if adopted else None) or row.lang,
        publisher=row.publisher or "",
        team_label=row.label or "",
        right_to_left=True if row.reading_direction == "rtl" else None,
        age=age,
        people=_people(connection, row.work_id),
        notes=notes,
        web=web,
        year=year,
        month=month,
        day=day,
    )  # fmt: skip


def comicinfo_xml(connection: Connection, unit_id: str, series: str) -> bytes:
    """The ComicInfo to write; what the writer had to change is said in the log."""
    rendered = render_comicinfo(comicinfo_data(connection, unit_id, series))
    for warning in rendered.warnings:
        log.warning("export.comicinfo_warning", unit_id=unit_id, detail=warning)
    return rendered.xml


def comicinfo_sha256(connection: Connection, unit_id: str, series: str) -> str:
    xml = render_comicinfo(comicinfo_data(connection, unit_id, series)).xml
    return "sha256:" + hashlib.sha256(xml).hexdigest()


class CbzRenderer:
    def __init__(self, fsops: Fsops, *, staging_dir: Path) -> None:
        self._fsops = fsops
        self._staging = staging_dir

    def render(self, source: str, comicinfo: bytes) -> RenderedCbz:
        out = tempfile.TemporaryFile(dir=self._staging)  # noqa: SIM115 (closed by the caller)
        try:
            with (
                self._fsops.open_read(source) as stream,
                safe_open_archive(stream, name=PurePath(source).name) as archive,
            ):
                original = content_hash(archive)
                repackage_cbz(archive, comicinfo, out)
            out.flush()
            out.seek(0)
            digest = hashlib.sha256()
            while chunk := out.read(1 << 20):
                digest.update(chunk)
            size = out.tell()
            out.seek(0)
            with safe_open_archive(out, name="rendered.cbz") as rendered:
                payload = content_hash(rendered)
            if payload != original:
                raise RenderMismatchError("the rendered pages differ from the received ones")
            out.seek(0)
        except BaseException:
            out.close()
            raise
        return RenderedCbz(
            out, size, "sha256:" + digest.hexdigest(), payload,
            "sha256:" + hashlib.sha256(comicinfo).hexdigest(),
        )  # fmt: skip


@dataclass
class RenderedEpub:
    file: BinaryIO  # positioned at 0, closed by the caller
    size: int
    file_hash: str  # `sha256:` of the rendered archive
    original_opf: bytes  # for `keep_origin_opf` (/data/meta), never written to the file
    opf_changed: bool


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def epub_opf_edit(connection: Connection, unit_id: str, series: str) -> OpfEdit:
    """What may be written into the OPF of a Unit's EPUB (EF-EXP-033): the series, its index, and
    the rights of the Edition (text and origin URL) for the OPF that has none. The age is not here
    and cannot be: it lives in the database and reaches Kavita through its API (D1, L09)."""
    unit = catalog.unit
    row = connection.execute(
        sa.select(unit.c.number_norm, unit.c.track, unit.c.edition_id).where(unit.c.id == unit_id)
    ).one()
    number = (
        row.number_norm
        if row.track == "main" and _NUMBER.fullmatch(row.number_norm or "")
        else None
    )
    notes, web = _rights(connection, row.edition_id)
    return OpfEdit(
        series=series, series_index=number, rights=notes or None, source=web or None,
        licence=licence_of(notes),
    )  # fmt: skip


class EpubRenderer:
    """The EPUB written for a destination: the file as the library holds it, its OPF edited.

    `source` must be the file `servable_epub` names (M2 L03): the verified derivative of an
    `untrusted` EPUB, or the file as filed for a `publisher` one. This class never chooses: it
    does not know what trust is, and an original from /store must never reach it. The caller that
    places the file also calls `keep_origin_opf` with `RenderedEpub.original_opf`.
    """

    def __init__(self, fsops: Fsops | None = None, *, staging_dir: Path) -> None:
        self._fsops = fsops
        self._staging = staging_dir

    def render(self, source: str, edit: OpfEdit) -> RenderedEpub:
        out = tempfile.TemporaryFile(dir=self._staging)  # noqa: SIM115 (closed by the caller)
        try:
            name = PurePath(source).name
            with ExitStack() as stack:
                stream = stack.enter_context(
                    self._fsops.open_read(source) if self._fsops else open(source, "rb")  # noqa: SIM115
                )
                archive = stack.enter_context(safe_open_archive(stream, name=name))
                before = _entry_digests(archive)
                done = repackage_epub(archive, edit, out)
            out.flush()
            out.seek(0)
            with safe_open_archive(out, name="rendered.epub") as rendered:
                after = _entry_digests(rendered)
            if {k: v for k, v in before.items() if k != done.opf_path} != {
                k: v for k, v in after.items() if k != done.opf_path
            }:
                raise RenderMismatchError("an entry other than the OPF differs from the source")
            out.seek(0)
            digest = hashlib.sha256()
            while chunk := out.read(1 << 20):
                digest.update(chunk)
            size = out.tell()
            out.seek(0)
        except BaseException:
            out.close()
            raise
        return RenderedEpub(
            out, size, "sha256:" + digest.hexdigest(), done.original_opf, done.changed
        )


def _entry_digests(archive: SafeArchive) -> dict[str, str]:
    found: dict[str, str] = {}
    for entry in archive.entries:
        digest = hashlib.sha256()
        if not entry.is_dir:
            with archive.open(entry.name) as data:
                while chunk := data.read(1 << 20):
                    digest.update(chunk)
        found[entry.name] = digest.hexdigest()
    return found
