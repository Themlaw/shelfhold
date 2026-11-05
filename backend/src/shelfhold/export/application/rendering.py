"""The CBZ written for a destination: the pages as received, a ComicInfo made for it (EF-EXP-011).

The ComicInfo is built from the database (never from the upstream document), the pages are copied
from the audited received archive, and the payload is checked again on the way out: the rendered
archive must have the `content_hash` of the original, so that only the sidecar differs (G1).
"""

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, BinaryIO

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.packaging.cbz import repackage_cbz
from shelfhold.infrastructure.packaging.comicinfo import ComicInfoData, render_comicinfo
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.library.public import content_hash
from shelfhold.sdk.age import AgeRating

__all__ = ["CbzRenderer", "RenderedCbz", "comicinfo_data", "comicinfo_sha256"]


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


def comicinfo_data(connection: Connection, unit_id: str, series: str) -> ComicInfoData:
    unit, edition, work, team = catalog.unit, catalog.edition, catalog.work, catalog.team
    variant = catalog.unit_variant
    row = connection.execute(
        sa.select(
            unit.c.title, unit.c.number_norm, unit.c.track, edition.c.lang, edition.c.publisher,
            work.c.sort_title, work.c.reading_direction, work.c.age_rating, work.c.age_x,
            team.c.label,
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
    age = AgeRating(18, True) if row.age_x else None
    if age is None and row.age_rating is not None:
        age = AgeRating(int(row.age_rating))
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
    )  # fmt: skip


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
