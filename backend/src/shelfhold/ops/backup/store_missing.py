"""A restore without `/store`: the Units kept there become `store_missing`, nothing is deleted.

Run by `reconcile --adopt-after-restore` after the adoption (EF-INT-015, matrix row
`store_missing_after_restore`). A Unit in state `stored` whose kept copy (`content.path`, relative
to `/store`) is not a file under the store folder is marked `store_missing`; the Unit, its
contents and every row stay, and no file is touched. A `/store` that is not mounted gives the
same answer as one that was lost, and the answer is the safe one: the Unit waits for its copy
(`store reinit`, or the host restoring `/store`) instead of being served or dropped.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.schema import catalog

__all__ = ["mark_store_missing"]


def _present(store: Path, rel: str) -> bool:
    parts = rel.split("/")
    if rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return False
    try:
        return stat.S_ISREG(os.lstat(store.joinpath(*parts)).st_mode)
    except OSError:
        return False


def mark_store_missing(engine: Engine, store_dir: Path, bus: EventBus) -> int:
    """The number of Units marked. Only `stored` Units move; one with a copy elsewhere
    (`exported`, `adopted`) keeps its state."""
    unit = catalog.unit
    # a view of two columns: the storage tables are read by the library adapters alone
    content = sa.table("content", sa.column("unit_id"), sa.column("path"))
    marked: list[str] = []
    with engine.begin() as connection:
        rows = connection.execute(
            sa.select(unit.c.id, content.c.path)
            .select_from(unit.join(content, content.c.unit_id == unit.c.id))
            .where(unit.c.state == "stored", content.c.path.is_not(None))
        ).all()
        kept: dict[str, bool] = {}
        for unit_id, rel in rows:
            kept[unit_id] = kept.get(unit_id, True) and _present(store_dir, str(rel))
        marked = [unit_id for unit_id, ok in kept.items() if not ok]
        for unit_id in marked:
            connection.execute(
                sa.update(unit).where(unit.c.id == unit_id, unit.c.state == "stored")
                .values(state="store_missing")
            )  # fmt: skip
        payload = {"kind": "store_missing", "count": len(marked)}
        published = [bus.publish(connection, "reconcile.drift", payload)] if marked else []
    bus.deliver(published)
    return len(marked)
