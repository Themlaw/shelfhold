"""Kavita scans that could not be asked, kept until they are asked again.

A scan lost on the way is a series Kavita keeps listing after its files left. Two queues wait in
`app_setting` (`kavita.scans_pending`: remote paths; `deletion.folders_pending_scan`: the
`<destination>/<folder>` of a deleted series), each a map `entry -> attempts`. Every change is a
read-modify-write under `BEGIN IMMEDIATE` that touches only its own entries, so a scan written down
by another writer in the meantime is never lost. A path Kavita refuses for good counts an attempt;
past `MAX_ATTEMPTS` it leaves the queue with an event the admin sees (`kavita.scan_abandoned`).
Only the unavailability of Kavita stops a pass.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import platform

__all__ = [
    "FOLDERS_KEY",
    "MAX_ATTEMPTS",
    "PENDING_SCANS_KEY",
    "ScanRefusedError",
    "ScanUnavailableError",
    "load_pending_scans",
    "remember_entries",
    "remember_scans",
    "settle_entries",
]

PENDING_SCANS_KEY: Final = "kavita.scans_pending"
FOLDERS_KEY: Final = "deletion.folders_pending_scan"
MAX_ATTEMPTS: Final = 5


class ScanUnavailableError(Exception):
    """Kavita cannot be asked now (unreachable, asleep, paused, no key): the pass stops and the
    scans wait for the next one."""


class ScanRefusedError(Exception):
    """Kavita (or the path map) refuses this path for good: only this path is concerned."""


def load_entries(connection: Connection, key: str) -> dict[str, int]:
    value = connection.execute(
        sa.select(platform.app_setting.c.value).where(platform.app_setting.c.key == key)
    ).scalar()
    if not value:
        return {}
    data = json.loads(value)
    if isinstance(data, list):  # the shape before the counters
        return {str(x): 0 for x in data}
    return {str(k): int(v) for k, v in data.items()}


def _save(connection: Connection, key: str, entries: dict[str, int]) -> None:
    t = platform.app_setting
    connection.execute(sa.delete(t).where(t.c.key == key))
    if entries:
        connection.execute(sa.insert(t).values(key=key, value=json.dumps(entries)))


def remember_entries(engine: Engine, key: str, entries: Iterable[str]) -> None:
    """Written down, once each; the counter of an entry already there is kept."""
    with write_transaction(engine) as connection:
        held = load_entries(connection, key)
        for entry in entries:
            held.setdefault(entry, 0)
        _save(connection, key, held)


def settle_entries(
    engine: Engine, key: str, *, done: Iterable[str] = (), refused: Iterable[str] = ()
) -> list[str]:
    """Forget what was done, count an attempt for what was refused, and drop (and return) what
    went past `MAX_ATTEMPTS`. Re-read under the write lock: nothing else is touched."""
    abandoned: list[str] = []
    with write_transaction(engine) as connection:
        held = load_entries(connection, key)
        for entry in done:
            held.pop(entry, None)
        for entry in refused:
            if entry in held:
                held[entry] += 1
                if held[entry] >= MAX_ATTEMPTS:
                    del held[entry]
                    abandoned.append(entry)
        _save(connection, key, held)
    return abandoned


def load_pending_scans(connection: Connection) -> list[str]:
    return list(load_entries(connection, PENDING_SCANS_KEY))


def remember_scans(engine: Engine, paths: Iterable[str]) -> None:
    remember_entries(engine, PENDING_SCANS_KEY, paths)
