"""Kavita scans that could not be asked (Kavita unreachable), kept until they are asked again.

A scan lost on the way is a series Kavita keeps listing after its files left. The remote paths
wait in `app_setting` (`kavita.scans_pending`); `LibraryOps.retry_pending_scans` asks them again
on each export run and forgets them once every Kavita target has been asked.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import platform

__all__ = ["PENDING_SCANS_KEY", "load_pending_scans", "remember_scans", "replace_pending_scans"]

PENDING_SCANS_KEY: Final = "kavita.scans_pending"


def load_pending_scans(connection: Connection) -> list[str]:
    value = connection.execute(
        sa.select(platform.app_setting.c.value).where(
            platform.app_setting.c.key == PENDING_SCANS_KEY
        )
    ).scalar()
    return [str(x) for x in json.loads(value)] if value else []


def replace_pending_scans(connection: Connection, paths: list[str]) -> None:
    t = platform.app_setting
    connection.execute(sa.delete(t).where(t.c.key == PENDING_SCANS_KEY))
    if paths:
        connection.execute(sa.insert(t).values(key=PENDING_SCANS_KEY, value=json.dumps(paths)))


def remember_scans(engine: Engine, paths: Iterable[str]) -> None:
    with write_transaction(engine) as connection:
        held = load_pending_scans(connection)
        replace_pending_scans(connection, held + [p for p in dict.fromkeys(paths) if p not in held])
