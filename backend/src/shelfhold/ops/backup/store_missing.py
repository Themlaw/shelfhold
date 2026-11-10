"""A restore without `/store`: the Units kept there wait as `store_missing`, nothing is deleted.

Run by `reconcile --adopt-after-restore` after the adoption (EF-INT-015, matrix row
`store_missing_after_restore`). Two cases, told apart by the sentinel of `/store`
(`.shelfhold-mount-id`, the file every registered root carries):

- **No sentinel** (the volume is not mounted, or is a different, empty one): `/store` is *lost or
  absent*, not "some files are missing". Nothing is marked: the state is `store_lost` (the exit of
  `shelfhold store reinit`, EF-EXP-064), reported through `reconcile.drift` with `kind=store_lost`
  and the number of Units that depend on it. A mount that comes back is then enough.
- **Sentinel present** but a kept copy absent: the `stored` Unit becomes `store_missing`
  (`reconcile.drift`, `kind=store_missing`); the Unit, its contents and every row stay, and no
  file is touched. A `store_missing` Unit whose copy is back with the right digest (the host
  restored `/store` afterwards) becomes `stored` again.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.schema import catalog

__all__ = ["StoreOutcome", "mark_store_missing"]

SENTINEL = ".shelfhold-mount-id"


@dataclass(frozen=True)
class StoreOutcome:
    marked: int
    restored: int
    lost: bool
    dependent: int = 0


def _present(store: Path, rel: str) -> bool:
    parts = rel.split("/")
    if rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return False
    try:
        return stat.S_ISREG(os.lstat(store.joinpath(*parts)).st_mode)
    except OSError:
        return False


def _sentinel_present(store: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(store / SENTINEL).st_mode)
    except OSError:
        return False


def _digest(path: Path) -> str | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def mark_store_missing(engine: Engine, store_dir: Path, bus: EventBus) -> StoreOutcome:
    """Only `stored` Units move to `store_missing`; one with a copy elsewhere (`exported`,
    `adopted`) keeps its state."""
    unit = catalog.unit
    # a view of the columns needed: the storage tables are read by the library adapters alone
    content = sa.table(
        "content",
        sa.column("unit_id"),
        sa.column("path"),
        sa.column("original_sha256"),
        sa.column("derived_hash"),
    )
    published = []
    with engine.begin() as connection:
        rows = connection.execute(
            sa.select(unit.c.id, unit.c.state, content.c.path, content.c.original_sha256,
                      content.c.derived_hash)
            .select_from(unit.join(content, content.c.unit_id == unit.c.id))
            .where(unit.c.state.in_(("stored", "store_missing")), content.c.path.is_not(None))
        ).all()  # fmt: skip
        if not _sentinel_present(store_dir):
            dependent = len({r.id for r in rows if r.state == "stored"})
            payload = {"kind": "store_lost", "count": dependent}
            published = [bus.publish(connection, "reconcile.drift", payload)] if dependent else []
            outcome = StoreOutcome(0, 0, True, dependent)
        else:
            present: dict[str, bool] = {}
            for row in rows:
                if row.state == "stored":
                    ok = _present(store_dir, str(row.path))
                else:
                    ok = _back(store_dir, row)
                present[row.id] = present.get(row.id, True) and ok
            states = {r.id: r.state for r in rows}
            marked = [i for i, ok in present.items() if not ok and states[i] == "stored"]
            restored = [i for i, ok in present.items() if ok and states[i] == "store_missing"]
            for unit_id in marked:
                connection.execute(
                    sa.update(unit).where(unit.c.id == unit_id, unit.c.state == "stored")
                    .values(state="store_missing")
                )  # fmt: skip
            for unit_id in restored:
                connection.execute(
                    sa.update(unit).where(unit.c.id == unit_id, unit.c.state == "store_missing")
                    .values(state="stored")
                )  # fmt: skip
            payload = {"kind": "store_missing", "count": len(marked)}
            published = [bus.publish(connection, "reconcile.drift", payload)] if marked else []
            outcome = StoreOutcome(len(marked), len(restored), False)
    bus.deliver(published)
    return outcome


def _back(store: Path, row: object) -> bool:
    """The copy is there again, and is the file the database knows."""
    if not _present(store, str(row.path)):  # type: ignore[attr-defined]
        return False
    found = _digest(store.joinpath(*str(row.path).split("/")))  # type: ignore[attr-defined]
    expected = {row.original_sha256, row.derived_hash}  # type: ignore[attr-defined]
    return found is not None and found in expected
