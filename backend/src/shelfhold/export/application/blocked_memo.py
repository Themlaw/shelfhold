"""What a pass learned from the disk and need not learn again (ENF-OPS-014).

A candidate whose destination is taken is blocked by a look at the disk. The look itself wakes
the disks, and the answer cannot change until the inputs change (the plan version) or the
window verification has looked at the tree anyway. So the version of the plan that came out
empty is remembered per destination, in `app_setting` (a key, no schema of its own): while the
plan of the database alone has that version, the pass stops before the disks. The window
verification forgets it, and so does any change of the inputs, which changes the version.
"""

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.schema import platform

_PREFIX = "export.blocked."
_table = platform.app_setting


def _key(destination_id: str) -> str:
    return f"{_PREFIX}{destination_id}"


def known(engine: Engine, destination_id: str, version: str) -> bool:
    with engine.connect() as connection:
        value = connection.execute(
            sa.select(_table.c.value).where(_table.c.key == _key(destination_id))
        ).scalar()
    return value == version


def remember(engine: Engine, destination_id: str, version: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            sqlite_insert(_table)
            .values(key=_key(destination_id), value=version)
            .on_conflict_do_update(index_elements=["key"], set_={"value": version})
        )


def forget(engine: Engine, destination_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(sa.delete(_table).where(_table.c.key == _key(destination_id)))
