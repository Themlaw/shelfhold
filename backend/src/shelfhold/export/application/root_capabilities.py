"""What each physical root can do, kept for `shelfhold status` (EF-EXP-059).

The server probes every writable root at start (`Fsops.probe_capabilities`) and writes the answer
here: one `app_setting` row per root, replaced at each start (no migration). `status` runs in
another process, without the lease, and must not touch a root: it reads these rows.
"""

from __future__ import annotations

import json
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.fsops import RootCapabilities
from shelfhold.infrastructure.schema import platform

__all__ = ["read_all", "save"]

PREFIX: Final = "capabilities."


def save(connection: Connection, root_id: str, capabilities: RootCapabilities) -> None:
    value = json.dumps(capabilities.as_dict(), sort_keys=True)
    connection.execute(
        sqlite_insert(platform.app_setting)
        .values(key=f"{PREFIX}{root_id}", value=value)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )


def read_all(connection: Connection) -> list[RootCapabilities]:
    table = platform.app_setting
    rows = connection.execute(
        sa.select(table.c.value).where(table.c.key.like(f"{PREFIX}%")).order_by(table.c.key)
    )
    found: list[RootCapabilities] = []
    for row in rows:
        try:
            found.append(RootCapabilities.from_dict(json.loads(row.value)))
        except (ValueError, KeyError, TypeError):
            continue  # a row we cannot read is a root not probed, never a failing `status`
    return sorted(found, key=lambda c: c.root)
