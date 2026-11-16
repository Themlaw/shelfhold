"""Who wants a shelf paused for `child_exposure` (EF-EXP-047, ENF-EXP-055, #320 M5).

The column `shelf.paused_reason` is one flag; several audits (Kavita's, Komga's, one per target)
may each want it set. A pause is therefore recorded **per (shelf, auditor)**: one `app_setting` row
per auditor (`pause.child_exposure.<target>`) holding the shelves it wants paused. An audit lifts
only its own hold, and the flag falls only when no auditor holds the shelf any more. A pause that
nobody holds (set by a change of ceiling or of path map, or made before holds existed) is lifted by
an audit only when it is told it may (`lift_unheld`).
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.export.application.target_state import CHILD_EXPOSURE, pause_shelf, resume_shelf
from shelfhold.infrastructure.schema import export, platform

__all__ = ["held_by", "hold", "holders", "release"]

_PREFIX = "pause.child_exposure."


def held_by(connection: Connection, auditor: str) -> set[str]:
    raw = connection.execute(
        sa.select(platform.app_setting.c.value).where(
            platform.app_setting.c.key == _PREFIX + auditor
        )
    ).scalar()
    try:
        loaded = json.loads(raw) if raw is not None else []
    except ValueError:
        return set()
    return {s for s in loaded if isinstance(s, str)} if isinstance(loaded, list) else set()


def _store(connection: Connection, auditor: str, shelves: set[str]) -> None:
    table = platform.app_setting
    value = json.dumps(sorted(shelves))
    done = connection.execute(
        sa.update(table).where(table.c.key == _PREFIX + auditor).values(value=value)
    )
    if done.rowcount == 0:
        connection.execute(sa.insert(table).values(key=_PREFIX + auditor, value=value))


def holders(connection: Connection, shelf_id: str) -> set[str]:
    """Every auditor that holds this shelf."""
    table = platform.app_setting
    found = set()
    for row in connection.execute(
        sa.select(table.c.key, table.c.value).where(table.c.key.like(_PREFIX + "%"))
    ):
        auditor = row.key[len(_PREFIX) :]
        if shelf_id in held_by(connection, auditor):
            found.add(auditor)
    return found


def hold(connection: Connection, auditor: str, shelf_id: str) -> bool:
    """This auditor wants the shelf paused. True when it is news: the hold is new, or the flag had
    to be set again."""
    mine = held_by(connection, auditor)
    new = shelf_id not in mine
    if new:
        _store(connection, auditor, mine | {shelf_id})
    return pause_shelf(connection, shelf_id, CHILD_EXPOSURE) or new


def release(connection: Connection, auditor: str, shelf_id: str, *, lift_unheld: bool) -> bool:
    """This auditor no longer wants the shelf paused. True when the pause was lifted: nobody else
    holds it and either this auditor did, or `lift_unheld` says an unheld pause may fall."""
    mine = held_by(connection, auditor)
    was_mine = shelf_id in mine
    if was_mine:
        _store(connection, auditor, mine - {shelf_id})
    if holders(connection, shelf_id):
        return False
    if not (was_mine or lift_unheld):
        return False
    return resume_shelf(connection, shelf_id, CHILD_EXPOSURE)


def other_targets_of(connection: Connection, shelf_id: str, target_id: str) -> bool:
    """True when the shelf is also bound to a target other than this one."""
    d = export.shelf_destination
    return bool(
        connection.execute(
            sa.select(sa.func.count()).where(d.c.shelf_id == shelf_id, d.c.target_id != target_id)
        ).scalar_one()
    )
