"""Physical roots as the database knows them (`physical_root`, L04 / EF-EXP-059, EF-EXP-060).

One row is one physical root: its `root_id`, its path (`path_entry`) and its mode. The path map
points to `root_id` through the root that contains each entry. The database mode wins over the
configuration: an entry inside an `adopt_ro` root is read-only whatever the configured path map
says, and such a root gets no writable `ExpectedRoot`, so fsops never writes it and no lease is
ever taken on it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.export.domain.path_map import PathEntry, PathMap, PathMode, clean_absolute, within
from shelfhold.export.domain.physical_roots import ExpectedRoot
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import platform

__all__ = ["LoadedRoots", "load_roots", "open_fsops"]


@dataclass(frozen=True)
class RootRow:
    root_id: str
    path: str
    mode: PathMode
    generation: int
    predecessors: tuple[str, ...] = ()  # root ids this root had before (`root takeover`)


@dataclass(frozen=True)
class LoadedRoots:
    path_map: PathMap
    expected: list[ExpectedRoot]
    roots: tuple[RootRow, ...]
    freeze: FreezeState = field(default_factory=FreezeState)  # persisted, until the adoption

    @property
    def restore_pending(self) -> bool:
        """`restored_behind`: the freeze of the database (EF-INT-026), until the adoption."""
        return self.freeze.frozen

    def locate(self, local: str) -> tuple[str, str]:
        """`(root id, path relative to that root)` of a local path."""
        root = self.root_id_for(local)
        if root is None:
            raise ValueError(local)
        base = next(r.path for r in self.roots if r.root_id == root)
        return root, local.removeprefix(base.rstrip("/") + "/")

    def root_id_for(self, local: str) -> str | None:
        """The `root_id` of the physical root that contains `local`."""
        found = _containing(self.roots, clean_absolute(local))
        return found.root_id if found else None


def _containing(roots: tuple[RootRow, ...], path: str) -> RootRow | None:
    best: RootRow | None = None
    for row in roots:
        if within(path, row.path) is not None and (best is None or len(row.path) > len(best.path)):
            best = row
    return best


def load_roots(connection: Connection, path_map: PathMap) -> LoadedRoots:
    head = connection.execute(
        sa.select(platform.instance.c.instance_id, platform.instance.c.restore_epoch)
    ).one()
    instance_id = head.instance_id
    table = platform.physical_root
    roots = tuple(
        RootRow(
            r.root_id,
            clean_absolute(r.path_entry),
            PathMode(r.mode),
            r.generation,
            tuple(p for p in (r.predecessor_instance_ids or []) if isinstance(p, str)),
        )
        for r in connection.execute(sa.select(table).order_by(table.c.path_entry))
    )
    entries: list[PathEntry] = []
    for entry in path_map.entries:
        root = _containing(roots, entry.local)
        mode = PathMode.ADOPT_RO if root and root.mode is PathMode.ADOPT_RO else entry.mode
        entries.append(PathEntry(entry.local, entry.remote, mode))
    expected = [
        ExpectedRoot(r.root_id, r.path, instance_id, r.generation, head.restore_epoch)
        for r in roots
        if r.mode is PathMode.RW
    ]
    return LoadedRoots(PathMap(tuple(entries)), expected, roots, FreezeState.read(connection))


def open_fsops(
    loaded: LoadedRoots,
    *,
    umask: int,
    write_gate: Callable[[], None] | None = None,
    stop_gate: Callable[[], None] | None = None,
) -> Fsops:
    """The one way to open the library's `Fsops`: the gates a caller has (disk, stop barrier), the
    restore freeze (`FreezeState`, one copy per writer) and the library umask
    (`SHELFHOLD_UMASK`, EF-EXP-006), which no assembly may forget: `umask` has no default."""
    return Fsops.open(
        loaded.path_map, loaded.expected, umask=umask, write_gate=write_gate, stop_gate=stop_gate,
        freeze=loaded.freeze.fork(),
    )  # fmt: skip
