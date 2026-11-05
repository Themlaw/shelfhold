"""The permission preflight: can this process write where it is about to export? (EF-EXP-054)

A probe file is created, renamed and removed by the uid of the container, in the root of a
destination before any export and in each series folder before the pairing. A failure comes back
with its cause (the `errno`, the owner and the mode of the folder, the uid and gid we run as) and
the exact commands that fix it. A write refused later does not retry in a loop: the series waits
in `permission_denied` (event `target.permission_denied`, once) until `target recheck`.

Only the closed `probe_file_cleanup` emitter removes what the probe made, and only a file whose
name the probe itself drew (the perimeter is enforced by fsops).
"""

from __future__ import annotations

import contextlib
import errno
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.events.bus import EventBus
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.emitters import new_probe_name
from shelfhold.infrastructure.schema import export

__all__ = [
    "DENIED",
    "DestinationNotWritableError",
    "FolderProbe",
    "Owner",
    "PermissionPreflight",
    "ProbeResult",
    "RecheckReport",
    "fix_commands",
]

DENIED: Final = "permission_denied"  # `export_series.paused_reason`, the waiting state
EVENT: Final = "target.permission_denied"
STEPS: Final = ("write", "rename", "remove")


@dataclass(frozen=True)
class Owner:
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    folder: str
    steps: tuple[str, ...]  # what worked, in order
    failed_step: str | None
    cause: str | None  # `EACCES`, `EROFS`, `ENOSPC`... or the class of an unexpected error
    runs_as: tuple[int, int]
    owner: Owner | None
    fix: str  # the commands, empty when the probe passed

    def explain(self) -> str:
        """The message of a refusal: what failed, where, who owns it, and what to run."""
        if self.ok:
            return f"{self.folder}: writable"
        owner = (
            f"owner {self.owner.uid}:{self.owner.gid}, mode {self.owner.mode:04o}"
            if self.owner
            else "owner unknown"
        )
        return (
            f"{self.folder}: {self.failed_step} refused ({self.cause}); {owner}; "
            f"Shelfhold runs as {self.runs_as[0]}:{self.runs_as[1]}.\n{self.fix}"
        )


class DestinationNotWritableError(OSError):
    """The root of a destination refuses the probe: nothing is exported (code
    `target.permission_denied`)."""

    code = EVENT

    def __init__(self, probe: ProbeResult) -> None:
        super().__init__(probe.explain())
        self.probe = probe


@dataclass(frozen=True)
class RecheckReport:
    cleared: tuple[str, ...]  # export series that can be written again
    denied: tuple[str, ...]  # export series still waiting


def _errno_name(error: OSError) -> str:
    if error.errno is not None and error.errno in errno.errorcode:
        return errno.errorcode[error.errno]
    return type(error).__name__


def fix_commands(folder: str, runs_as: tuple[int, int], owner: Owner | None) -> str:
    """The exact commands, for the path as Shelfhold sees it (use the host path on the host)."""
    uid, gid = runs_as
    lines = [
        "To fix, on the host (replace the path by the host path of this folder):",
        f"  setfacl -R -m u:{uid}:rwX -d -m u:{uid}:rwX '{folder}'",
    ]
    if owner is not None and owner.gid != gid:
        lines.append(
            f"  or add the group of the folder to the container (compose: group_add: "
            f"[\"{owner.gid}\"]) and run: chmod -R g+rwX '{folder}'"
        )
    if owner is not None:
        lines.append(
            f'  or run the container as the owner (compose: user: "{owner.uid}:{owner.gid}")'
        )
    lines.append("  and give new files group write access (umask 002 for the writer).")
    return "\n".join(lines)


def _owner_of(path: str) -> Owner | None:
    try:
        seen = os.lstat(path)
    except OSError:
        return None
    return Owner(seen.st_uid, seen.st_gid, seen.st_mode & 0o7777)


class FolderProbe:
    """Create, rename and remove a probe file in a folder; leaves nothing behind."""

    def __init__(
        self,
        fsops: Fsops,
        *,
        runs_as: tuple[int, int] | None = None,
        owner_of: Callable[[str], Owner | None] = _owner_of,
    ) -> None:
        self._fsops: Fsops = fsops
        self._runs_as = runs_as
        self._owner_of = owner_of

    def _remove_probe_file(self, path: str) -> None:
        self._fsops.system_unlink("probe_file_cleanup", path)

    def run(self, folder: str) -> ProbeResult:
        ids = self._runs_as or (os.geteuid(), os.getegid())
        base = folder.rstrip("/")
        first, second = f"{base}/{new_probe_name()}", f"{base}/{new_probe_name()}"
        done: list[str] = []
        current: str | None = None
        step = "write"
        try:
            self._fsops.write_atomic(first, b"shelfhold probe\n", parents=False, replace=False)
            current = first
            done.append("write")
            step = "rename"
            self._fsops.rename(first, second)
            current = second
            done.append("rename")
            step = "remove"
            self._remove_probe_file(second)
            current = None
            done.append("remove")
        except OSError as error:
            owner = self._owner_of(base)
            return ProbeResult(
                False, folder, tuple(done), step, _errno_name(error), ids, owner,
                fix_commands(folder, ids, owner),
            )  # fmt: skip
        finally:
            if current is not None:  # a step failed with the probe file in place: take it away
                with contextlib.suppress(OSError):  # the cause is reported; a leftover is swept
                    self._remove_probe_file(current)
        return ProbeResult(True, folder, tuple(done), None, None, ids, None, "")


class PermissionPreflight:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        bus: EventBus,
        *,
        clock: Clock,
        runs_as: tuple[int, int] | None = None,
        owner_of: Callable[[str], Owner | None] = _owner_of,
    ) -> None:
        self._engine = engine
        self._prober = FolderProbe(fsops, runs_as=runs_as, owner_of=owner_of)
        self._bus = bus
        self._clock = clock

    def probe(self, folder: str) -> ProbeResult:
        return self._prober.run(folder)

    def _context(self, destination_id: str) -> DestinationContext:
        with self._engine.connect() as connection:
            return load_context(connection, destination_id)

    def check_root(self, destination_id: str) -> ProbeResult | None:
        """The root of the destination, or None when it has no folder on this machine."""
        ctx = self._context(destination_id)
        return self.probe(ctx.root_local) if ctx.root_local else None

    def require_root(self, destination_id: str) -> None:
        """Before any export: uid, write and rename on the root, or a clear refusal."""
        result = self.check_root(destination_id)
        if result is not None and not result.ok:
            raise DestinationNotWritableError(result)

    def series_folder(self, destination_id: str, export_series_id: str) -> str | None:
        ctx = self._context(destination_id)
        with self._engine.connect() as connection:
            directory = connection.execute(
                sa.select(export.export_series.c.series_dir).where(
                    export.export_series.c.id == export_series_id
                )
            ).scalar()
        if not directory or ctx.root_local is None:
            return None
        return f"{ctx.root_local}/{directory}"

    def check_series(self, destination_id: str, export_series_id: str) -> ProbeResult | None:
        """The folder of a series; None when the series has none yet (the root decides then)."""
        folder = self.series_folder(destination_id, export_series_id)
        return self.probe(folder) if folder else None

    # -- the waiting state ---------------------------------------------------------------------

    def mark_denied(self, destination_id: str, export_series_id: str) -> bool:
        """A write was refused: the series waits, announced once, never retried by itself."""
        series = export.export_series
        with self._engine.begin() as connection:
            changed = connection.execute(
                sa.update(series)
                .where(series.c.id == export_series_id, series.c.paused_reason.is_(None))
                .values(paused_reason=DENIED)
            ).rowcount
            if changed:
                self._announce(connection, destination_id, export_series_id)
        return bool(changed)

    def _announce(self, connection: Connection, destination_id: str, series_id: str) -> None:
        self._bus.publish(
            connection, EVENT, {"destination_id": destination_id, "export_series_id": series_id}
        )

    def denied_series(self, destination_id: str) -> list[str]:
        series = export.export_series
        with self._engine.connect() as connection:
            return list(
                connection.execute(
                    sa.select(series.c.id)
                    .where(
                        series.c.shelf_destination_id == destination_id,
                        series.c.paused_reason == DENIED,
                    )
                    .order_by(series.c.id)
                ).scalars()
            )

    def recheck(self, destination_id: str) -> RecheckReport:
        """`target recheck`: probe every waiting series, free those that can be written."""
        cleared: list[str] = []
        denied: list[str] = []
        series = export.export_series
        for series_id in self.denied_series(destination_id):
            result = self.check_series(destination_id, series_id)
            if result is None or result.ok:
                with self._engine.begin() as connection:
                    connection.execute(
                        sa.update(series)
                        .where(series.c.id == series_id, series.c.paused_reason == DENIED)
                        .values(paused_reason=None)
                    )
                cleared.append(series_id)
            else:
                denied.append(series_id)
        return RecheckReport(tuple(cleared), tuple(denied))
