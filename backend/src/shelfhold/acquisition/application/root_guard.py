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

The path is the one Suwayomi reports, in its own namespace. It is compared three ways: as it is
(when both see the folder at one path, or when it exists here), translated by the path map of each
target (`remote` -> `local`, the usual Docker case: Suwayomi mounts the folder under `/manga`,
Shelfhold under `/library/manga`), and against the `remote` side of every path map. A path that
none of these relates, and that does not exist here, cannot be told apart from a managed root: a
native folder is refused for it (`LocalSourceUnknownError`) rather than believed apart.
"""

import json
import os
import posixpath
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.export.public import (
    LocalSourceUnknownError,
    PathMapError,
    Relation,
    RootOverlapsLocalSourceError,
    absolute_relation,
    clean_absolute,
    folder_relation,
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

    @staticmethod
    def _entries(connection: Connection) -> list[tuple[str, str]]:
        """The `(local, remote)` pairs of every path map: what the readers see, and where."""
        pairs: list[tuple[str, str]] = []
        for path_map in connection.execute(sa.select(export.export_target.c.path_map)).scalars():
            for e in path_map or []:
                if isinstance(e, dict) and e.get("local") and e.get("remote"):
                    pairs.append((str(e["local"]), str(e["remote"])))
        return pairs

    @staticmethod
    def _tidy(path: str) -> str:
        """A path of Suwayomi's namespace, cleaned by its text alone (it is not ours to resolve):
        `..` folded, no trailing slash. What cannot be cleaned is kept as it is."""
        try:
            return clean_absolute(posixpath.normpath(path))
        except PathMapError:
            return posixpath.normpath(path)

    @staticmethod
    def _below(path: str, base: str) -> str | None:
        """What is under `base` in `path`, by components and lax names, or None."""
        parts, root = [p for p in path.split("/") if p], [p for p in base.split("/") if p]
        if len(parts) < len(root) or folder_relation(parts[: len(root)], root).value not in (
            "same",
            "lookalike",
        ):
            return None
        return "/".join(parts[len(root) :])

    def _local_views(self, connection: Connection, reported: str) -> tuple[list[str], bool]:
        """Where a path of Suwayomi is, in the namespace of Shelfhold: the path itself, and the
        places the path maps put it. The flag says whether that is known: the folder exists here,
        or a path map covers it (it may not exist yet; it is still said where it will be)."""
        clean = self._tidy(reported)
        views = [reported]
        translated = False
        for local, remote in self._entries(connection):
            rest = self._below(clean, clean_absolute(remote))
            if rest is not None:
                translated = True
                views.append(clean_absolute(f"{local}/{rest}" if rest else local))
        return views, translated or os.path.exists(reported)

    def _remote_related(self, connection: Connection, reported: str) -> bool:
        """The path Suwayomi reports against the `remote` side of every path map: a folder it
        reads that holds or sits in what a reader sees as a managed root."""
        parts = [p for p in self._tidy(reported).split("/") if p]
        for _local, remote in self._entries(connection):
            if (
                folder_relation(parts, [p for p in clean_absolute(remote).split("/") if p]).value
                != "apart"
            ):
                return True
        return False

    # -- the local source read from Suwayomi ---------------------------------------------------
    def check_local_source(self, connection: Connection, path: str) -> None:
        views, _known = self._local_views(connection, path)
        roots = self.managed_roots(connection)
        if self._remote_related(connection, path) or any(
            self._related(view, root) for view in views for root in roots
        ):
            raise LocalSourceOnManagedRootError()

    # -- a native folder root about to be bound, or moved ----------------------------------------
    def check_root(self, connection: Connection, root: str) -> None:
        for known in self.known(connection):
            if known is None:
                raise LocalSourceUnknownError(
                    "Suwayomi is configured and its local source is unread or unreadable"
                )
            views, placed = self._local_views(connection, known)
            if any(self._related(root, view) for view in views):
                raise RootOverlapsLocalSourceError(
                    "the root holds or sits in the local source of Suwayomi"
                )
            if self._remote_of_root_related(connection, root, known):
                raise RootOverlapsLocalSourceError(
                    "the root holds or sits in the local source of Suwayomi"
                )
            if not placed:
                raise LocalSourceUnknownError(
                    "the local source of Suwayomi is at a path that this instance cannot see, "
                    "nor translate through a path map: it cannot be told apart from this folder"
                )

    def _remote_of_root_related(self, connection: Connection, root: str, reported: str) -> bool:
        """The root as its readers see it (`local` -> `remote`) against the reported path."""
        clean_root = self._tidy(root)
        reported_parts = [p for p in self._tidy(reported).split("/") if p]
        for local, remote in self._entries(connection):
            rest = self._below(clean_root, clean_absolute(local))
            if rest is None:
                continue
            seen = clean_absolute(f"{remote}/{rest}" if rest else remote)
            if folder_relation(reported_parts, [p for p in seen.split("/") if p]).value != "apart":
                return True
        return False

    @staticmethod
    def known(connection: Connection) -> list[str | None]:
        """What the follow last read of each Suwayomi source: its local source path, or None for
        a source that was configured and never read, or whose last answer could not be read (an
        absent field, a relative path, a control character). A source that was read and says it
        has no local source is not in the list."""
        table = acquisition.source_config
        rows = connection.execute(
            sa.select(table.c.health).where(table.c.plugin_id == SUWAYOMI)
        ).all()
        out: list[str | None] = []
        for row in rows:
            seen = (row.health or {}).get(_KEY) if isinstance(row.health, dict) else None
            if not isinstance(seen, dict) or seen.get("unreadable"):
                out.append(None)  # never read, or read but not understood: unknown, not apart
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

    @staticmethod
    def record_unreadable(connection: Connection, plugin_id: str) -> None:
        """What a cycle could not read: unknown, which is not the same as having no local source."""
        table = acquisition.source_config
        connection.execute(
            sqlite_insert(table).values(plugin_id=plugin_id).on_conflict_do_nothing()
        )
        connection.execute(
            sa.text(
                "UPDATE source_config SET health = json_set(coalesce(health, '{}'), "
                "'$.local_source', json(:value)) WHERE plugin_id = :plugin"
            ),
            {"value": json.dumps({"unreadable": True}), "plugin": plugin_id},
        )
