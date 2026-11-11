"""Which chapters an adopted volume carries (EF-BIB-044, EF-EXP-053, `adopt set-coverage`).

An adopted volume file covers its chapters; until they are known, it is a Unit of its own with no
range and the series cannot be followed by itself (`coverage_unknown`). The range comes from the
admin (`set-coverage`, origin `manual`) or from the `Vol.N` prefix of the chapter names of the
source (origin `source_name`, written by the follow cycle). Both put chapter Units under the
volume's `export_item`, so that presence (I20) skips them: `backfill --gaps` asks for none.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.export.application import waiting_marks
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export

__all__ = [
    "MAX_RANGE",
    "AdoptedVolume",
    "CoverageError",
    "adopted_volumes",
    "apply_comicinfo_range",
    "attach_chapter",
    "attach_to_volume",
    "comicinfo_range",
    "flag_unknown_coverage",
    "has_chapter_files",
    "set_volume_coverage",
    "volume_number_of",
]

MAX_RANGE: Final = 2000  # chapters under one volume: a typo (`1 100000`) must not fill the table


class CoverageError(ValueError):
    """The volume or the range given is not one (refused as `ops.invalid_choice`)."""


@dataclass(frozen=True)
class AdoptedVolume:
    number: str
    export_item_id: str
    known: bool  # at least one chapter is under it


def volume_number_of(label: str) -> str | None:
    """`Vol.3`, `Vol 3`, `Volume 3` -> `3`; anything else is no volume label."""
    text = label.strip().casefold()
    for prefix in ("volume", "vol."):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    else:
        if not text.startswith("vol"):
            return None
        text = text[3:]
    text = text.strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    return format(number.normalize(), "f")


def adopted_volumes(connection: Connection, edition_id: str) -> list[AdoptedVolume]:
    item, cov, unit = export.export_item, export.unit_coverage, catalog.unit
    rows = connection.execute(
        sa.select(item.c.id, item.c.frozen_attrs)
        .where(item.c.edition_id == edition_id, item.c.kind == "adopted")
        .order_by(item.c.id)
    ).all()
    found: list[AdoptedVolume] = []
    for row in rows:
        attrs = row.frozen_attrs or {}
        if attrs.get("kind") != "volume":
            continue
        chapters = connection.execute(
            sa.select(sa.func.count())
            .select_from(cov.join(unit, unit.c.id == cov.c.unit_id))
            .where(
                cov.c.carrier_type == "export_item",
                cov.c.carrier_id == row.id,
                unit.c.track == "main",
            )
        ).scalar_one()
        found.append(AdoptedVolume(str(attrs.get("number_norm", "")), row.id, bool(chapters)))
    return found


def has_chapter_files(connection: Connection, edition_id: str) -> bool:
    unit = catalog.unit
    return (
        connection.execute(
            sa.select(unit.c.id)
            .where(
                unit.c.edition_id == edition_id, unit.c.track == "main", unit.c.state == "adopted"
            )
            .limit(1)
        ).first()
        is not None
    )


def flag_unknown_coverage(connection: Connection, export_series_id: str, edition_id: str) -> bool:
    """Write down (or erase) `coverage_unknown` for an adopted series: no chapter at all, or a
    volume whose range is not known. Returns whether the series waits."""
    unknown = not has_chapter_files(connection, edition_id) or any(
        not v.known for v in adopted_volumes(connection, edition_id)
    )
    if unknown:
        waiting_marks.put(
            connection, "coverage_unknown", export_series_id,
            "the chapters of an adopted volume are not known",
        )  # fmt: skip
    else:
        waiting_marks.clear(connection, "coverage_unknown", [export_series_id])
    return unknown


def _chapter_unit(connection: Connection, edition_id: str, number: str) -> str:
    unit = catalog.unit
    found = connection.execute(
        sa.select(unit.c.id).where(
            unit.c.edition_id == edition_id,
            unit.c.track == "main",
            unit.c.number_segment == "",
            unit.c.number_norm == number,
            unit.c.part == "",
        )
    ).scalar()
    if found is not None:
        return str(found)
    unit_id = new_ulid()
    connection.execute(
        sa.insert(unit).values(
            id=unit_id, edition_id=edition_id, number_raw=number, number_norm=number,
            number_sort=float(number), track="main", state="adopted",
        )
    )  # fmt: skip
    return unit_id


def _held_elsewhere(connection: Connection, edition_id: str, number: str, item_id: str) -> bool:
    """The chapter exists and an export item other than `item_id` already covers it."""
    unit, cov = catalog.unit, export.unit_coverage
    return (
        connection.execute(
            sa.select(cov.c.unit_id)
            .select_from(cov.join(unit, unit.c.id == cov.c.unit_id))
            .where(
                unit.c.edition_id == edition_id,
                unit.c.track == "main",
                unit.c.number_segment == "",
                unit.c.number_norm == number,
                unit.c.part == "",
                cov.c.carrier_type == "export_item",
                cov.c.carrier_id != item_id,
            )
            .limit(1)
        ).first()
        is not None
    )


def attach_chapter(
    connection: Connection, item_id: str, unit_id: str, *, origin: str, create: bool = True
) -> bool:
    """Put one more Unit under an adopted volume. `False`: it already is there."""
    cov = export.unit_coverage
    present = connection.execute(
        sa.select(cov.c.position).where(
            cov.c.carrier_type == "export_item",
            cov.c.carrier_id == item_id,
            cov.c.unit_id == unit_id,
        )
    ).first()
    if present is not None:
        return False
    top = connection.execute(
        sa.select(sa.func.max(cov.c.position)).where(
            cov.c.carrier_type == "export_item", cov.c.carrier_id == item_id
        )
    ).scalar()
    connection.execute(
        sa.insert(cov).values(
            carrier_type="export_item", carrier_id=item_id, unit_id=unit_id,
            position=0 if top is None else int(top) + 1, extent="full", origin=origin,
        )
    )  # fmt: skip
    return True


def set_volume_coverage(
    connection: Connection, edition_id: str, volume: str, first: str, last: str,
    *, origin: str = "manual",
) -> int:  # fmt: skip
    """Chapters `first` to `last` (whole numbers) are what the adopted volume `volume` holds.
    Returns the number of chapters newly put under it."""
    try:
        if Decimal(first) != Decimal(first).to_integral_value() or (
            Decimal(last) != Decimal(last).to_integral_value()
        ):
            raise CoverageError("the first and the last chapter are whole numbers")
        low, high = int(Decimal(first)), int(Decimal(last))
        wanted = format(Decimal(volume).normalize(), "f")
    except (InvalidOperation, ValueError):
        raise CoverageError("the volume and the chapters are numbers") from None
    if low < 0 or high < low or high - low >= MAX_RANGE:
        raise CoverageError(f"the range goes from 0 to {MAX_RANGE} chapters, first <= last")
    target = next((v for v in adopted_volumes(connection, edition_id) if v.number == wanted), None)
    if target is None:
        raise LookupError(f"no adopted volume {volume} in this series")
    taken = [
        str(n)
        for n in range(low, high + 1)
        if _held_elsewhere(connection, edition_id, str(n), target.export_item_id)
    ]
    if taken:  # two files never share a Unit: nothing is written
        raise CoverageError(f"chapters already carried by another file: {', '.join(taken)}")
    added = 0
    for number in range(low, high + 1):
        unit_id = _chapter_unit(connection, edition_id, str(number))
        added += attach_chapter(connection, target.export_item_id, unit_id, origin=origin)
    return added


_RANGE: Final = re.compile(r"(\d{1,7})\s*[-\u2013]\s*(\d{1,7})")


def comicinfo_range(info: Mapping[str, str]) -> tuple[str, str, str] | None:
    """`(volume, first, last)` from the ComicInfo of an adopted volume: `Volume` is the volume's
    number and `Number` the chapters it holds, as a range (`41-50`). Anything else says nothing
    (the range is then asked of the admin, never guessed)."""
    wanted = volume_number_of(f"vol {info.get('Volume', '')}")
    found = _RANGE.fullmatch(info.get("Number", "").strip())
    if wanted is None or found is None:
        return None
    first, last = found.groups()
    if int(last) < int(first):
        return None
    return wanted, first, last


def apply_comicinfo_range(connection: Connection, edition_id: str, info: Mapping[str, str]) -> int:
    """Put the chapters a ComicInfo names under its adopted volume (origin `comicinfo`). Returns
    the number of chapters newly put there; 0 when the ComicInfo names no range."""
    named = comicinfo_range(info)
    if named is None:
        return 0
    return set_volume_coverage(connection, edition_id, *named, origin="comicinfo")


def attach_to_volume(connection: Connection, edition_id: str, unit_id: str, label: str) -> bool:
    """A chapter whose name starts with `Vol.N` goes under the adopted volume N of its Edition
    (origin `source_name`). `True`: it is under one now, new or already; there is nothing to
    download for it."""
    number = volume_number_of(label)
    if number is None:
        return False
    for volume in adopted_volumes(connection, edition_id):
        if volume.number == number:
            held = connection.execute(
                sa.select(export.unit_coverage.c.carrier_id).where(
                    export.unit_coverage.c.unit_id == unit_id,
                    export.unit_coverage.c.carrier_type == "export_item",
                    export.unit_coverage.c.carrier_id != volume.export_item_id,
                )
            ).first()
            if held is not None:  # another file carries it already: never two
                return False
            attach_chapter(connection, volume.export_item_id, unit_id, origin="source_name")
            return True
    return False
