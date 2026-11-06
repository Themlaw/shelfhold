"""What waits for a human after an adoption, kept in the database (EF-INT-023, EF-EXP-053).

`adoption_refused` (a shelf that looks fed by Suwayomi, EF-EXP-044) and `coverage_unknown` (an
adopted series whose chapters cannot be told, EF-BIB-044) are decided when the library is read
and when a series is paired; `status` must show them in a later process, so they are written
down: one `app_setting` row per subject (no migration), deleted when the way out is taken.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Final, NamedTuple

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.schema import platform

__all__ = ["MARKED_STATES", "Mark", "clear", "put", "read_all"]

PREFIX: Final = "waiting."
MARKED_STATES: Final = ("adoption_refused", "coverage_unknown")


class Mark(NamedTuple):
    state: str
    subject: str
    reason: str


def _key(state: str, subject: str) -> str:
    return f"{PREFIX}{state}.{subject}"


def put(connection: Connection, state: str, subject: str, reason: str) -> None:
    if state not in MARKED_STATES:
        raise ValueError(f"{state} is not a persisted waiting state")
    value = json.dumps({"reason": reason})
    connection.execute(
        sqlite_insert(platform.app_setting)
        .values(key=_key(state, subject), value=value)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )


def clear(connection: Connection, state: str, subjects: Iterable[str]) -> None:
    keys = [_key(state, s) for s in subjects]
    if keys:
        connection.execute(
            sa.delete(platform.app_setting).where(platform.app_setting.c.key.in_(keys))
        )


def read_all(connection: Connection) -> list[Mark]:
    table = platform.app_setting
    rows = connection.execute(
        sa.select(table.c.key, table.c.value)
        .where(table.c.key.like(f"{PREFIX}%"))
        .order_by(table.c.key)
    )
    found: list[Mark] = []
    for row in rows:
        state, _, subject = row.key[len(PREFIX) :].partition(".")
        if state in MARKED_STATES:
            found.append(Mark(state, subject, str(json.loads(row.value).get("reason", state))))
    return found
