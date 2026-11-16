"""The slice of a block: how many chapters a new part may hold (M4 L49b, ADR-0053 §1 and §8).

A slice is the admin's choice for an Edition, read at each request. It only narrows the cut of a
part (the bytes and the members of `BundleLimits` still apply) and it concerns the parts that are
not written yet: a boundary written stays where it is (L49a), so a version a reader holds keeps
its part. `describe` reads the manifest and the written boundaries and writes nothing.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import bundle, catalog
from shelfhold.reading.bundle import plan as planning
from shelfhold.reading.bundle.limits import BundleLimits

__all__ = [
    "SLICE_MAX",
    "SLICE_MIN",
    "Described",
    "PartView",
    "describe",
    "edition_exists",
    "get_slice",
    "save",
    "set_slice",
]

SLICE_MIN: Final = 1
SLICE_MAX: Final = 256


def edition_exists(connection: Connection, edition_id: str) -> bool:
    found = connection.execute(
        sa.select(catalog.edition.c.id).where(catalog.edition.c.id == edition_id)
    ).first()
    return found is not None


def get_slice(connection: Connection, edition_id: str) -> int | None:
    table = bundle.bundle_setting
    return connection.execute(
        sa.select(table.c.slice_chapters).where(table.c.edition_id == edition_id)
    ).scalar_one_or_none()


def set_slice(connection: Connection, edition_id: str, chapters: int | None, now: datetime) -> None:
    """Call in a write transaction. `None` removes the row: the cut is the one of the bytes."""
    table = bundle.bundle_setting
    if chapters is None:
        connection.execute(sa.delete(table).where(table.c.edition_id == edition_id))
        return
    if not SLICE_MIN <= chapters <= SLICE_MAX:
        raise ValueError("a slice holds between 1 and 256 chapters")
    held = connection.execute(
        sa.update(table)
        .where(table.c.edition_id == edition_id)
        .values(slice_chapters=chapters, updated_at=now)
    ).rowcount
    if not held:
        connection.execute(
            sa.insert(table).values(edition_id=edition_id, slice_chapters=chapters, updated_at=now)
        )


def save(engine: Engine, edition_id: str, chapters: int | None, now: datetime) -> None:
    with write_transaction(engine) as connection:
        set_slice(connection, edition_id, chapters, now)


@dataclass(frozen=True)
class PartView:
    index: int
    chapters: int
    first_number: str
    last_number: str
    size: int | None
    written: bool  # its boundary is written: a slice no longer moves it


@dataclass(frozen=True)
class Described:
    slice_chapters: int | None
    parts: list[PartView]
    excluded: dict[str, int]


def describe(connection: Connection, edition_id: str, limits: BundleLimits) -> Described:
    candidates = planning.load_candidates(connection, edition_id)
    eligible = [c for c in candidates if c.left_out is None]
    chosen = get_slice(connection, edition_id)
    stored = planning.stored_boundaries(connection, edition_id)
    parts = planning.plan_parts(stored, eligible, limits, chosen)
    written = {index for index, _unit in stored}
    excluded = {"format": 0, "unsafe": 0}
    for c in candidates:
        if c.left_out is not None:
            excluded[c.left_out] += 1
    return Described(
        chosen,
        [
            PartView(
                p.index,
                len(p.members),
                p.members[0].number,
                p.members[-1].number,
                None
                if any(m.size is None for m in p.members)
                else sum(m.size or 0 for m in p.members),
                p.index in written,
            )
            for p in parts
        ],
        excluded,
    )
