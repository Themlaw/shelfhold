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
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.freeze import ROOT_STATES, WAITING_PREFIX
from shelfhold.infrastructure.schema import platform

__all__ = [
    "MARKED_STATES",
    "Mark",
    "clear",
    "clear_except",
    "clear_mount_blocked",
    "mark_mount_blocked",
    "put",
    "read_all",
    "read_one",
]

PREFIX: Final = WAITING_PREFIX
MARKED_STATES: Final = (
    "adoption_refused",
    "coverage_unknown",
    "series_name_collision",
    "mount_sentinel_missing",
    *ROOT_STATES,  # EF-EXP-064: written by `FreezeState.persist_root`, listed like the others
)


class Mark(NamedTuple):
    state: str
    subject: str
    reason: str


def _key(state: str, subject: str) -> str:
    return f"{PREFIX}{state}.{subject}"


def put(connection: Connection, state: str, subject: str, reason: str, **extra: str) -> None:
    """`extra`: what the exit needs to know about the subject (the destination, for instance)."""
    if state not in MARKED_STATES:
        raise ValueError(f"{state} is not a persisted waiting state")
    value = json.dumps({"reason": reason, **extra})
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


def mark_mount_blocked(engine: Engine, destination_id: str, reason: str) -> None:
    """The root of a destination fails its sentinel, or lost too many files (EF-EXP-029): the
    exports, deletions and scans stay blocked and `status` says so until it is right again."""
    with engine.begin() as connection:
        put(
            connection, "mount_sentinel_missing", destination_id,
            {"sentinel": "the mount does not carry the expected sentinel",
             "missing_ratio": "too many files of the library are missing"}.get(reason, reason),
            destination_id=destination_id,
        )  # fmt: skip


def clear_mount_blocked(engine: Engine, destination_id: str) -> None:
    with engine.begin() as connection:
        clear(connection, "mount_sentinel_missing", [destination_id])


def read_one(connection: Connection, state: str, subject: str) -> dict[str, str] | None:
    raw = connection.execute(
        sa.select(platform.app_setting.c.value).where(
            platform.app_setting.c.key == _key(state, subject)
        )
    ).scalar()
    return None if raw is None else {k: str(v) for k, v in json.loads(raw).items()}


def clear_except(connection: Connection, state: str, scope: str, keep: Iterable[str]) -> None:
    """Drop the marks of `state` written for `scope` (a destination) whose subject is not kept."""
    kept = set(keep)
    for found in read_all(connection):
        if found.state != state or found.subject in kept:
            continue
        one = read_one(connection, state, found.subject)
        if one is not None and one.get("destination_id") == scope:
            clear(connection, state, [found.subject])


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
