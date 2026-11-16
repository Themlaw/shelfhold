"""The guard against the local source of Suwayomi (EF-EXP-010, M4 L07).

Suwayomi reads a folder as its *local source*. A folder that Shelfhold writes (the root of a native
folder destination, `/store`, the library) must never be that folder, nor hold it, nor sit
inside it:
Shelfhold would follow its own exports as an upstream and acquire them again, for ever. The check
runs in both directions and is one rule (`export.public.absolute_relation`, the rule of `folders.py`
that bindings and the executor already use): by components, never by string prefix; one folder under
another case, another Unicode form, a trailing dot or space is the same; a link, a `..` and a bind
mount (`st_dev`, `st_ino`, of the folder or an ancestor) too.

- `check_local_source`: what the cycle reads from Suwayomi, against every root Shelfhold manages.
  Refused: `LocalSourceOnManagedRootError`, which pauses the follow with a typed code.
- `check_root`: a native folder root about to be bound, against the local source Suwayomi last said.
  Refused: `RootOverlapsLocalSourceError`. A Suwayomi that was configured and never read:
  `LocalSourceUnknownError` (fail closed). A Suwayomi that does not expose it: nothing to compare.

The path is the one Suwayomi reports, read as this process sees it: the two must mount the folder
at the same path (the same convention as the download folder of EF-EXP-044).
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.export.public import (
    LocalSourceUnknownError,
    Relation,
    RootOverlapsLocalSourceError,
    absolute_relation,
)
from shelfhold.infrastructure.schema import acquisition, export, platform
from shelfhold.plugin_host.errors import LocalSourceOnManagedRootError

__all__ = ["FolderFacts", "RootGuard", "folder_facts"]

SUWAYOMI: Final = "suwayomi"  # the plugin id of the connected service
_KEY: Final = "local_source"


@dataclass(frozen=True)
class FolderFacts:
    """A folder as the disk shows it: its real path, its `(st_dev, st_ino)` and the ones of every
    folder from the top down to it (itself included), for the folders that exist."""

    real: str
    identity: tuple[int, int] | None
    ancestry: frozenset[tuple[int, int]]


def _identity(path: str) -> tuple[int, int] | None:
    try:
        observed = os.stat(path)
    except (OSError, ValueError):
        return None
    return observed.st_dev, observed.st_ino


def folder_facts(path: str) -> FolderFacts:
    real = os.path.realpath(path)
    chain: set[tuple[int, int]] = set()
    probe = real
    while True:
        found = _identity(probe)
        if found is not None:
            chain.add(found)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return FolderFacts(real, _identity(real), frozenset(chain))


class RootGuard:
    def __init__(self, *, facts: Callable[[str], FolderFacts] = folder_facts) -> None:
        self.facts = facts

    # -- what is managed ---------------------------------------------------------------------
    @staticmethod
    def managed_roots(connection: Connection) -> list[str]:
        """Every folder Shelfhold writes or owns: the physical roots (the library, `/store`, the
        roots of destinations) and the local folders of the path map of every target."""
        paths = list(connection.execute(sa.select(platform.physical_root.c.path_entry)).scalars())
        for path_map in connection.execute(sa.select(export.export_target.c.path_map)).scalars():
            paths += [
                str(e["local"]) for e in path_map or [] if isinstance(e, dict) and e.get("local")
            ]
        return sorted({p for p in paths if p.startswith("/")})

    def _related(self, one: str, other: str) -> bool:
        a, b = self.facts(one), self.facts(other)
        how = absolute_relation(
            a.real, b.real, identity_a=a.identity, ancestry_a=a.ancestry,
            identity_b=b.identity, ancestry_b=b.ancestry,
        )  # fmt: skip
        return how is not Relation.APART

    # -- the local source read from Suwayomi ---------------------------------------------------
    def check_local_source(self, connection: Connection, path: str) -> None:
        for root in self.managed_roots(connection):
            if self._related(path, root):
                raise LocalSourceOnManagedRootError()

    # -- a native folder root about to be bound ----------------------------------------------
    def check_root(self, connection: Connection, root: str) -> None:
        for known in self.known(connection):
            if known is None:
                raise LocalSourceUnknownError(
                    "Suwayomi is configured and its local source is unread"
                )
            if self._related(root, known):
                raise RootOverlapsLocalSourceError(
                    "the root holds or sits in the local source of Suwayomi"
                )

    @staticmethod
    def known(connection: Connection) -> list[str | None]:
        """What the follow last read of each Suwayomi source: its local source path, or None for
        a source that was configured and never read. A source that was read and exposes no local
        source is not in the list."""
        table = acquisition.source_config
        rows = connection.execute(
            sa.select(table.c.health).where(table.c.plugin_id == SUWAYOMI)
        ).all()
        out: list[str | None] = []
        for row in rows:
            seen = (row.health or {}).get(_KEY) if isinstance(row.health, dict) else None
            if not isinstance(seen, dict):
                out.append(None)
            elif seen.get("path"):
                out.append(str(seen["path"]))
        return out

    @staticmethod
    def record(connection: Connection, plugin_id: str, path: str | None) -> None:
        """What a cycle read: the path Suwayomi gave, or None when it gives none."""
        table = acquisition.source_config
        connection.execute(
            sqlite_insert(table).values(plugin_id=plugin_id).on_conflict_do_nothing()
        )
        value: dict[str, Any] = {"path": path}
        connection.execute(
            sa.text(
                "UPDATE source_config SET health = json_set(coalesce(health, '{}'), "
                "'$.local_source', json(:value)) WHERE plugin_id = :plugin"
            ),
            {"value": json.dumps(value), "plugin": plugin_id},
        )
