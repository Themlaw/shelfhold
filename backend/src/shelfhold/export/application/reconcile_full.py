"""The full verification of a tree destination: drift, orphans, duplicates, symbolic links
(EF-EXP-018, M3).

A **read-only inventory** next to the reduced pass of `reconcile.py` (which keeps its own
decisions: put back, ask, pause). This one only *reports*: nothing is moved, rewritten, deleted or
followed, and what the administrator keeps in the tree is never touched. Every read goes through
`Fsops` (a descriptor walk, `O_NOFOLLOW`, the declared root), never a path built by hand; a link is
reported as a link and not read through.

The hash is read inside the maintenance window only (ENF-OPS-014): outside it the pass compares
sizes and mtimes, and a duplicate, which needs the hash, waits for the window.
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Engine

from shelfhold.events.bus import EventBus
from shelfhold.export.application.intents import open_intent_paths
from shelfhold.export.application.reconcile import managed_files
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.domain.physical_roots import RESERVED_DIR, SENTINEL_FILE, RootState
from shelfhold.export.domain.reconcile import (
    LIVE_STATES,
    Change,
    ManagedFile,
    Seen,
    classify_change,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops

__all__ = ["FullReconciler", "InventoryReport"]

RESERVED_NAMES = frozenset({SENTINEL_FILE, RESERVED_DIR})
MAX_DEPTH = 8
# the `kind` of `reconcile.drift` for each family of findings
KINDS = {
    "drift": "modified_file",
    "orphans": "orphan_file",
    "duplicates": "duplicate_file",
    "symlinks": "symlink",
}


@dataclass(frozen=True)
class InventoryReport:
    """Relative paths, sorted. Empty when the pass was blocked or interrupted."""

    blocked: str | None = None  # `sentinel`: the root is not the one Shelfhold wrote, nothing read
    interrupted: bool = False  # a stop was asked: a partial view is never reported
    drift: tuple[str, ...] = ()  # managed files that are no longer what was written
    orphans: tuple[str, ...] = ()  # files nobody knows (not the manifest's, not a copy)
    duplicates: tuple[str, ...] = ()  # unknown files whose bytes equal another file's
    symlinks: tuple[str, ...] = ()  # links, described and never followed


class _Stopped(Exception):
    pass


class FullReconciler:
    def __init__(self, engine: Engine, fsops: Fsops, *, clock: Clock, bus: EventBus) -> None:
        self._engine = engine
        self._fsops = fsops
        self._clock = clock
        self._bus = bus
        self._announced: dict[str, dict[str, int]] = {}  # destination -> counts already told

    def scan(
        self,
        destination_id: str,
        *,
        in_window: bool = False,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> InventoryReport:
        """`in_window`: the hash may be read. `should_stop` is asked between two folders and two
        files: a stopped pass reports and announces nothing."""
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
            if ctx.transport != "tree" or ctx.root_local is None:
                return InventoryReport()
            managed = managed_files(connection, destination_id)
            covered = open_intent_paths(connection)
        root = ctx.root_local
        if self._fsops.status(root).state is not RootState.OK:
            return InventoryReport(blocked="sentinel")

        def skipped(rel: str) -> bool:
            path = f"{root}/{rel}"
            return any(path == c or path.startswith(c.rstrip("/") + "/") for c in covered)

        try:
            files, links = self._walk(root, should_stop, skipped)
            known = {f.rel_path: f for f in managed}
            drift = self._drift(root, managed, in_window, should_stop, skipped)
            untracked = {rel: size for rel, size in files.items() if rel not in known}
            duplicates = (
                self._duplicates(root, untracked, managed, should_stop) if in_window else set()
            )
        except _Stopped:
            return InventoryReport(interrupted=True)
        report = InventoryReport(
            drift=tuple(sorted(drift)),
            orphans=tuple(sorted(set(untracked) - duplicates)),
            duplicates=tuple(sorted(duplicates)),
            symlinks=tuple(sorted(links)),
        )
        self._announce(destination_id, report)
        return report

    # -- reading the tree, by `Fsops` only -----------------------------------------------------

    def _walk(
        self, root: str, should_stop: Callable[[], bool], skipped: Callable[[str], bool]
    ) -> tuple[dict[str, int], list[str]]:
        """`({file: size}, [links])` under `root`. A link is listed and never entered; a folder
        deeper than `MAX_DEPTH` is listed as one entry, not entered."""
        files: dict[str, int] = {}
        links: list[str] = []
        pending: list[tuple[str, int]] = [("", 0)]
        while pending:
            if should_stop():
                raise _Stopped
            rel_dir, depth = pending.pop()
            local = f"{root}/{rel_dir}" if rel_dir else root
            try:
                entries = self._fsops.list_dir(local)
            except (FileNotFoundError, NotADirectoryError):
                continue  # gone while it was read
            for name, kind in sorted(entries.items()):
                if not rel_dir and name in RESERVED_NAMES:
                    continue
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if skipped(rel):
                    continue
                if kind == "symlink":
                    links.append(rel)
                elif kind == "dir" and depth < MAX_DEPTH:
                    pending.append((rel, depth + 1))
                elif kind == "file":
                    try:
                        observed = self._fsops.observe(f"{root}/{rel}")
                    except OSError:  # swapped or gone while it was read: not a finding
                        continue
                    if observed is not None and observed[0] == "file":
                        files[rel] = observed[1].size
                else:  # a device, a socket, a folder too deep: an entry nobody knows
                    files[rel] = -1
        return files, links

    def _drift(
        self,
        root: str,
        managed: list[ManagedFile],
        in_window: bool,
        should_stop: Callable[[], bool],
        skipped: Callable[[str], bool],
    ) -> set[str]:
        """Managed files that are not what was written: another size or kind, or (in the window)
        other bytes. A file that is gone is the reduced pass's business, not drift."""
        found: set[str] = set()
        for item in managed:
            if item.state not in LIVE_STATES or skipped(item.rel_path):
                continue
            if should_stop():
                raise _Stopped
            local = f"{root}/{item.rel_path}"
            try:
                observed = self._fsops.observe(local)
                if observed is None:
                    continue
                kind, identity = observed
                dev = self._fsops.device_of(local) if kind == "file" else None
            except OSError:  # swapped or gone while it was read: the reduced pass's business
                continue
            change = classify_change(
                item,
                Seen(
                    identity.size,
                    identity.mtime_ns,
                    regular=kind == "file",
                    dev=dev,
                    ino=identity.ino,
                ),
            )
            if change is Change.SIZE_OR_KIND:
                found.add(item.rel_path)
            elif in_window and item.file_hash is not None:
                with contextlib.suppress(OSError):  # unreadable now: not a finding
                    if self._fsops.sha256_of(local) != item.file_hash:
                        found.add(item.rel_path)
        return found

    def _duplicates(
        self,
        root: str,
        untracked: dict[str, int],
        managed: list[ManagedFile],
        should_stop: Callable[[], bool],
    ) -> set[str]:
        """Unknown files with the bytes of another file. Only a file whose size another shares is
        read: a file alone at its size cannot be a copy. The manifest's hash stands for the files
        Shelfhold wrote, so those are not read again."""
        sizes: dict[int, int] = defaultdict(int)
        for size in untracked.values():
            sizes[size] += 1
        for item in managed:
            if item.size is not None:
                sizes[item.size] += 1
        by_hash: dict[str, set[str]] = defaultdict(set)
        for item in managed:
            if item.file_hash is not None and item.size is not None and sizes[item.size] > 1:
                by_hash[item.file_hash].add(f"managed:{item.rel_path}")
        for rel, size in sorted(untracked.items()):
            if size < 0 or sizes[size] < 2:
                continue
            if should_stop():
                raise _Stopped
            with contextlib.suppress(OSError):  # swapped for something unreadable: skipped
                by_hash[self._fsops.sha256_of(f"{root}/{rel}")].add(rel)
        return {
            rel
            for names in by_hash.values()
            if len(names) > 1
            for rel in names
            if not rel.startswith("managed:")
        }

    # -- telling -------------------------------------------------------------------------------

    def _announce(self, destination_id: str, report: InventoryReport) -> None:
        """One `reconcile.drift` per family, with its count and never a name, when the count
        changed since the last pass: a finding that stays is news once."""
        counts = {family: len(getattr(report, family)) for family in KINDS}
        last = self._announced.get(destination_id, {})
        self._announced[destination_id] = counts
        for family, kind in KINDS.items():
            if counts[family] and counts[family] != last.get(family):
                with self._engine.begin() as connection:
                    self._bus.publish(
                        connection, "reconcile.drift",
                        {"destination_id": destination_id, "kind": kind, "count": counts[family]},
                    )  # fmt: skip
