"""The intent journal (EF-EXP-041, ENF-OPS-019, ADR-0031).

Any file operation in several steps writes a `fs_intent` row and commits it *before* touching the
disk: `pending`, then `applying`, then the disk, then `done` in the same transaction as the manifest
update. At boot, before any job, `resolve_open` settles what a crash left open:

- an intent older than `instance.restore_epoch` (compared by the **counter**, never by the clock)
  came back with a restored snapshot: `stale_from_backup`, never replayed (EF-INT-026, I28, I39);
- an intent of a format this build does not know is left alone (ENF-OPS-019);
- otherwise the disk is looked at and the intent is replayed forward or completed **only if** it
  matches exactly one known step of the operation (source there and target absent, or target there
  with the expected hash/inode and source absent). Anything else is drift: the intent stays open,
  nothing is touched, and the caller reports it (`reconcile.drift`); nothing is ever re-exported.

Paths under an open intent are invisible to reconciliation and planning (I9, `skip_covered`).
"""

import contextlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.export.domain.provenance_mirror import MIRROR_DIR
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.fsops import FileIdentity, FsopsError
from shelfhold.infrastructure.fsops.confined import Kind
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, platform

INTENT_SCHEMA_VERSION = "1"  # frozen and versioned: ENF-OPS-019
KNOWN_SCHEMA_VERSIONS = frozenset({INTENT_SCHEMA_VERSION})
OPEN_STATES = ("pending", "applying")

__all__ = [
    "INTENT_SCHEMA_VERSION",
    "IntentJournal",
    "OpenIntent",
    "RenameStep",
    "ResolveReport",
    "open_intent_paths",
    "skip_covered",
]


class DiskPort(Protocol):
    """What the journal needs from `Fsops` (or a lease-checked write session)."""

    def rename(self, src: str, dst: str, *, replace: bool = False) -> None: ...
    def rmdir(self, local: str) -> None: ...
    def observe(self, local: str) -> tuple[Kind, FileIdentity] | None: ...
    def sha256_of(self, local: str) -> str: ...


@dataclass(frozen=True)
class OpenIntent:
    id: str
    op: str
    src: str | None
    dst: str | None
    expected_sha256: str | None
    expected_file_id: FileIdentity | None
    state: str
    epoch: int
    schema_version: str
    plan_id: str | None = None


@dataclass(frozen=True)
class RenameStep:
    src: str
    dst: str
    sha256: str | None = None  # `sha256:<hex>` when the manifest knows it


@dataclass
class ResolveReport:
    completed: list[str] = field(default_factory=list)  # target was there: manifest completed
    replayed: list[str] = field(default_factory=list)  # source was there: operation redone
    stale_from_backup: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # unknown schema version: left open
    drifted: list[str] = field(default_factory=list)  # unknown disk state: left open, untouched
    abandoned: list[str] = field(default_factory=list)  # a write that never reached the disk
    drift_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def still_open(self) -> list[str]:
        return [*self.refused, *self.drifted]


ManifestHook = Callable[[Connection, OpenIntent], None]


def _intent_from(row: sa.Row[tuple[object, ...]]) -> OpenIntent:
    file_id = row.expected_file_id
    return OpenIntent(
        id=row.id,
        op=row.op,
        src=row.src_path,
        dst=row.dst_path,
        expected_sha256=row.expected_sha256,
        expected_file_id=FileIdentity.from_json(file_id) if file_id else None,
        state=row.state,
        epoch=row.epoch,
        schema_version=row.intent_schema_version,
        plan_id=row.plan_id,
    )


def open_intent_paths(connection: Connection) -> frozenset[str]:
    """Every source and target path of an open intent (I9)."""
    table = coherence.fs_intent
    rows = connection.execute(
        sa.select(table.c.src_path, table.c.dst_path).where(table.c.state.in_(OPEN_STATES))
    )
    return frozenset(p for row in rows for p in (row.src_path, row.dst_path) if p)


def skip_covered(connection: Connection, candidates: Iterable[str]) -> list[str]:
    """The candidates not covered by an open intent: a path is covered when it is, or lies under,
    a path of one (a folder under `rmdir` covers what it holds)."""
    covered = open_intent_paths(connection)

    def is_covered(path: str) -> bool:
        return any(path == c or path.startswith(c.rstrip("/") + "/") for c in covered)

    return [path for path in candidates if not is_covered(path)]


class IntentJournal:
    def __init__(self, engine: Engine, disk: DiskPort, *, clock: Clock | None = None) -> None:
        self._engine = engine
        self._disk = disk
        self._clock = clock or SystemClock()

    # -- the protocol ---------------------------------------------------------------------------

    def record(
        self,
        *,
        op: str,
        src: str | None = None,
        dst: str | None = None,
        expected_sha256: str | None = None,
        expected_file_id: FileIdentity | None = None,
        plan_id: str | None = None,
    ) -> str:
        """Step 1: the intent, `pending`, committed before anything touches the disk."""
        intent_id = new_ulid()
        with self._engine.begin() as conn:
            epoch = conn.execute(sa.select(platform.instance.c.restore_epoch)).scalar_one()
            conn.execute(
                sa.insert(coherence.fs_intent).values(
                    id=intent_id,
                    op=op,
                    intent_schema_version=INTENT_SCHEMA_VERSION,
                    src_path=src,
                    dst_path=dst,
                    expected_sha256=expected_sha256,
                    expected_file_id=expected_file_id.to_json() if expected_file_id else None,
                    plan_id=plan_id,
                    state="pending",
                    epoch=epoch,
                )
            )
        return intent_id

    def mark_applying(self, intent_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(coherence.fs_intent)
                .where(coherence.fs_intent.c.id == intent_id)
                .where(coherence.fs_intent.c.state == "pending")
                .values(state="applying")
            )

    def mark_done(self, intent_id: str, *, within: Connection | None = None) -> None:
        """Step 4. Pass the connection of the manifest update to share its transaction."""
        statement = (
            sa.update(coherence.fs_intent)
            .where(coherence.fs_intent.c.id == intent_id)
            .where(coherence.fs_intent.c.state.in_(OPEN_STATES))
            .values(state="done", resolved_at=self._clock.now())
        )
        if within is not None:
            within.execute(statement)
            return
        with self._engine.begin() as conn:
            conn.execute(statement)

    def cancel(self, intent_id: str) -> None:
        """The operation failed before it touched the disk: nothing is left to settle."""
        self._close_open(intent_id, "cancelled")

    def apply_rename(self, step: RenameStep, *, on_done: ManifestHook | None = None) -> None:
        observed = self._disk.observe(step.src)
        if observed is None or observed[0] != "file":
            raise FileNotFoundError(step.src)
        file_id = observed[1]
        sha256 = step.sha256 or self._disk.sha256_of(step.src)  # replay needs the hash (02 6.7)
        intent_id = self.record(
            op="rename",
            src=step.src,
            dst=step.dst,
            expected_sha256=sha256,
            expected_file_id=file_id,
        )
        self.mark_applying(intent_id)
        self._disk.rename(step.src, step.dst)
        intent = OpenIntent(
            intent_id, "rename", step.src, step.dst, sha256, file_id, "applying",
            0, INTENT_SCHEMA_VERSION,
        )  # fmt: skip
        with self._engine.begin() as conn:
            self.mark_done(intent_id, within=conn)
            if on_done is not None:
                on_done(conn, intent)

    def apply_rename_plan(
        self, steps: Iterable[RenameStep], *, on_done: ManifestHook | None = None
    ) -> None:
        for step in steps:
            self.apply_rename(step, on_done=on_done)

    # -- boot -----------------------------------------------------------------------------------

    def resolve_open(self, *, on_done: ManifestHook | None = None) -> ResolveReport:
        """Before any job: settle every `pending`/`applying` intent, in the order they were made."""
        report = ResolveReport()
        table = coherence.fs_intent
        with self._engine.connect() as conn:
            current_epoch = conn.execute(sa.select(platform.instance.c.restore_epoch)).scalar_one()
            rows = conn.execute(
                sa.select(table)
                .where(table.c.state.in_(OPEN_STATES))
                .order_by(table.c.created_at, table.c.id)
            ).all()
        for row in rows:
            intent = _intent_from(row)
            if intent.epoch < current_epoch:  # the counter, not the wall clock (I39)
                self._close(intent.id, "stale_from_backup")
                report.stale_from_backup.append(intent.id)
            elif intent.schema_version not in KNOWN_SCHEMA_VERSIONS:
                report.refused.append(intent.id)
            else:
                self._resolve_one(intent, report, on_done)
        return report

    def _close_open(self, intent_id: str, state: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(coherence.fs_intent)
                .where(coherence.fs_intent.c.id == intent_id)
                .where(coherence.fs_intent.c.state.in_(OPEN_STATES))
                .values(state=state, resolved_at=self._clock.now())
            )

    def _close(self, intent_id: str, state: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(coherence.fs_intent)
                .where(coherence.fs_intent.c.id == intent_id)
                .values(state=state, resolved_at=self._clock.now())
            )

    def _drift(self, intent: OpenIntent, report: ResolveReport, reason: str) -> None:
        report.drifted.append(intent.id)
        report.drift_reasons[intent.id] = reason

    def _resolve_one(
        self, intent: OpenIntent, report: ResolveReport, on_done: ManifestHook | None
    ) -> None:
        try:
            if intent.op == "rename" and intent.src and intent.dst:
                self._resolve_rename(intent, report, on_done)
            elif intent.op == "rmdir" and intent.src:
                self._resolve_rmdir(intent, report, on_done)
            elif intent.op == "write" and intent.dst:
                self._resolve_write(intent, report, on_done)
            else:
                self._drift(intent, report, f"no replay rule for op {intent.op!r}")
        except (FsopsError, OSError, LookupError) as error:
            self._drift(intent, report, f"{type(error).__name__}: {error}")

    def _finish(self, intent: OpenIntent, on_done: ManifestHook | None) -> None:
        with self._engine.begin() as conn:
            self.mark_done(intent.id, within=conn)
            if on_done is not None:
                on_done(conn, intent)

    def _is_expected(self, intent: OpenIntent, path: str, identity: FileIdentity) -> bool:
        """The file at `path` is the one the intent was written for: the sha256 must match (an
        identity of inode, size and mtime can be forged by a rewrite in place), and the identity
        too when it was recorded. An intent without a hash is never replayed."""
        if intent.expected_sha256 is None:
            return False
        if intent.expected_file_id is not None and intent.expected_file_id != identity:
            return False
        return self._disk.sha256_of(path) == intent.expected_sha256

    def _resolve_rename(
        self, intent: OpenIntent, report: ResolveReport, on_done: ManifestHook | None
    ) -> None:
        if intent.src is None or intent.dst is None:
            return self._drift(intent, report, "a rename needs both paths")
        at_src = self._disk.observe(intent.src)
        at_dst = self._disk.observe(intent.dst)
        if at_src is not None and at_src[0] == "file" and at_dst is None:
            if not self._is_expected(intent, intent.src, at_src[1]):
                return self._drift(intent, report, "source is not the expected file")
            self._disk.rename(intent.src, intent.dst)
            self._finish(intent, on_done)
            report.replayed.append(intent.id)
        elif at_dst is not None and at_dst[0] == "file" and at_src is None:
            if not self._is_expected(intent, intent.dst, at_dst[1]):
                return self._drift(intent, report, "target is not provably the expected file")
            self._finish(intent, on_done)
            report.completed.append(intent.id)
        else:
            self._drift(intent, report, "neither one source-only nor one target-only state")

    def _resolve_write(
        self, intent: OpenIntent, report: ResolveReport, on_done: ManifestHook | None
    ) -> None:
        """A file Shelfhold was writing (02 section 5): the expected file is there, the manifest
        is completed (`on_done`); nothing is there, the write never happened and the intent is
        closed; anything else is somebody else's file or a damaged one, so nothing is touched."""
        if intent.dst is None:
            return self._drift(intent, report, "a write needs its target")
        found = self._disk.observe(intent.dst)
        mirror = f"/{MIRROR_DIR}/" in intent.dst  # a projection of the database (EF-EXP-063)
        if found is None:
            self._close(intent.id, "cancelled")
            report.abandoned.append(intent.id)
        elif found[0] == "file" and self._is_expected(intent, intent.dst, found[1]):
            self._finish(intent, on_done)
            report.completed.append(intent.id)
        elif mirror and found[0] == "file":
            # the previous mirror file, the write never landed (it is atomic): nothing to settle,
            # the next run or `meta rebuild` projects the database again
            self._close(intent.id, "cancelled")
            report.abandoned.append(intent.id)
        else:
            self._drift(intent, report, "the target is not the file that was being written")

    def _resolve_rmdir(
        self, intent: OpenIntent, report: ResolveReport, on_done: ManifestHook | None
    ) -> None:
        if intent.src is None:
            return self._drift(intent, report, "an rmdir needs its folder")
        found = self._disk.observe(intent.src)
        if found is None:
            self._finish(intent, on_done)
            report.completed.append(intent.id)
        elif found[0] == "dir":
            with contextlib.suppress(OSError):
                self._disk.rmdir(intent.src)  # only an empty folder goes
            if self._disk.observe(intent.src) is None:
                self._finish(intent, on_done)
                report.replayed.append(intent.id)
            else:
                self._drift(intent, report, "folder is not empty")
        else:
            self._drift(intent, report, "not a folder")
