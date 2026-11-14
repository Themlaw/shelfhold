"""The series names a target library already shows, whoever wrote them (EF-EXP-032).

The planner compares a proposed name with the series Shelfhold knows (`export_series`); a series
that Kavita holds from another source bears its name just the same. The export job reads the
library listing and keeps the normalized names here, one `app_setting` row per destination (no
migration), so that the plan, the dry run and `edition set-series-name` (another process) refuse
the same names.
"""

from __future__ import annotations

import json
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import platform

__all__ = ["load_foreign_names", "save_foreign_names"]

PREFIX: Final = "export.kavita_names."
MAX_NAMES: Final = 100_000


def save_foreign_names(engine: Engine, destination_id: str, names: list[str]) -> None:
    value = json.dumps(names[:MAX_NAMES])
    with write_transaction(engine) as connection:
        connection.execute(
            sqlite_insert(platform.app_setting)
            .values(key=PREFIX + destination_id, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )


def load_foreign_names(connection: Connection, destination_id: str) -> frozenset[str]:
    raw = connection.execute(
        sa.select(platform.app_setting.c.value).where(
            platform.app_setting.c.key == PREFIX + destination_id
        )
    ).scalar()
    if raw is None:
        return frozenset()
    try:
        return frozenset(str(n) for n in json.loads(raw))
    except ValueError:
        return frozenset()
