"""Restore epoch, `restored_behind` and `reconcile --adopt-after-restore` (EF-INT-026, EF-EXP-063).

**Detection** (`RestoreGuard`). At start-up every read-write root's sentinel is compared with the
database (generation and restore epoch). A database older than a sentinel is neither "OK" nor
"different": the instance enters `restored_behind`. The epoch counter moves (so that every
intent that came back with the backup is `stale_from_backup`, by counter, never by clock), the
event is announced once, and `Fsops` is frozen: no write, deletion, session or scan, on any root,
until the exit command.

**Exit** (`RestoreAdopter`). A read-only report first (files filed since the backup, intents gone
stale, files missing, upstream drift). Then a preview with a fingerprint, and the adoption only for
that fingerprint:

1. the tombstone journal is replayed **before** anything is adopted (I36); a root whose journal
   names a key we do not have, or cannot be read, blocks the adoption;
2. each line of each root's provenance mirror is judged (`judge_line`): a file whose hash is the
   line's, whose Units and Variants the restored database knows, and that is under no tombstone,
   takes back the state `written` (exported) with its lineage; anything else is kept and reported
   (`unresolved`: never erased), and what a tombstone covers is *proposed* for deletion;
3. the follow decisions ("stop following", restrictive) come back; ages come back as suggestions
   only, never as a rating;
4. the generations move past the sentinels', the sentinels are re-stamped, the instance thaws.

Adopting never deletes. A deletion that the replay asks for happens only after an admin confirmed
it (reserved action `reconcile.confirm_deletion`), through the purge's executor.
"""

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.events.bus import EventBus
from shelfhold.export.application.roots import LoadedRoots, load_roots
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.domain.export_item import CoverageError, check_coverage
from shelfhold.export.domain.path_map import PathMap, PathMode, clean_absolute, within
from shelfhold.export.domain.physical_roots import (
    MAX_RESTORE_EPOCH,
    RESERVED_DIR,
    SENTINEL_FILE,
    ExpectedRoot,
    judge_sentinel,
)
from shelfhold.export.domain.provenance_mirror import (
    Lineage,
    MirrorDocument,
    MirrorFormatError,
    grant_lineage,
)
from shelfhold.export.domain.restore import (
    LineFacts,
    LineOutcome,
    RestoreStatus,
    RootFinding,
    fingerprint_of,
    judge_line,
    safe_rel_path,
)
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.errors import FsopsError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import (
    acquisition,
    catalog,
    coherence,
    export,
    identity,
    platform,
    storage,
)
from shelfhold.library.public import EDITION_KEY_COLUMNS, record_destination_copy

__all__ = [
    "AdoptionBlockedError",
    "AdoptionPreview",
    "AdoptionResult",
    "DeletionCandidate",
    "DeletionConfirmation",
    "DeletionExecutor",
    "DeletionRefusedError",
    "DeletionUnavailableError",
    "PreviewStaleError",
    "ReadoptLine",
    "ReplayRoot",
    "RestoreAdopter",
    "RestoreGuard",
    "RestoreReport",
    "RestoredBehindError",
    "TombstoneScreen",
    "UpstreamInventory",
    "UpstreamSeries",
]

log = structlog.get_logger(__name__)

_SHA: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_NUMBER: Final = re.compile(r"^\d{1,9}(\.\d{1,6})?$")
_CHUNK: Final = 400
_DELETION = RESERVED_ACTIONS["reconcile.confirm_deletion"]
KIND_VALUES: Final = frozenset(export.EXPORT_ITEM_KINDS)
MAX_FIELD: Final = 512
MAX_DEPTH: Final = 8
_FIXED_REASONS: Final = frozenset({"destination_unknown", "path_escape"})
RESERVED_NAMES: Final = frozenset({SENTINEL_FILE, RESERVED_DIR})

ReplayRoot = tuple[str, str, bool, tuple[str, ...]]  # root_id, path, mounted, predecessor root ids


class RestoredBehindError(RuntimeError):
    """The database is older than the disk: nothing is written until the exit command."""

    code = "instance.restored_behind"


class PreviewStaleError(RuntimeError):
    """The confirmation is not the fingerprint of what the instance would do now."""


class AdoptionBlockedError(RuntimeError):
    """A root cannot be reconciled yet (tombstone key unknown, journal unreadable)."""

    def __init__(self, root_id: str, reason: str) -> None:
        super().__init__(f"root {root_id}: {reason}")
        self.root_id = root_id
        self.reason = reason


class DeletionRefusedError(PermissionError):
    """The principal may not confirm a deletion after a restore (reserved action)."""

    def __init__(self, verdict: object) -> None:
        super().__init__(f"reconcile.confirm_deletion refused: {type(verdict).__name__}")
        self.verdict = verdict


class DeletionUnavailableError(RuntimeError):
    """A deletion was confirmed but no purge executor is wired: nothing was changed."""


@dataclass(frozen=True)
class UpstreamSeries:
    source_key: str
    series_ref: str


class UpstreamInventory(Protocol):
    """The series of Suwayomi's Shelfhold category (read only; the connected service's job)."""

    def shelfhold_series(self) -> Sequence[UpstreamSeries]: ...


class TombstoneScreen(Protocol):
    """The tombstone journal, as the restore needs it: replayed first, then asked about."""

    def replay(self, roots: Sequence[ReplayRoot]) -> None: ...

    def root_blocked(self, root_id: str) -> str | None:
        """`key_unknown` or `journal_unreadable` when this root cannot be reconciled yet."""
        ...

    def unverified(self) -> int:
        """How many journal lines are inert (`tombstone.unverified`)."""
        ...

    def variant_blocked(
        self, root_id: str | None, plugin_id: str, source_key: str, remote_unit_id: str,
        content_hashes: Sequence[tuple[str, str]],
    ) -> bool: ...  # fmt: skip

    def content_blocked(
        self, root_id: str | None, content_hashes: Sequence[tuple[str, str]]
    ) -> bool:
        """A file without an upstream reference: its content alone."""
        ...

    def place_blocked(
        self, root_id: str | None, edition_key: Mapping[str, str], track: str, number_segment: str,
        number_raw: str, part: str,
    ) -> bool: ...  # fmt: skip

    def series_blocked(
        self, root_id: str | None, plugin_id: str, source_key: str, series_ref: str
    ) -> bool: ...


@dataclass(frozen=True)
class DeletionCandidate:
    """A file on disk that a tombstone covers: proposed, never deleted by the adoption."""

    destination_id: str
    root_id: str
    rel_path: str
    local_path: str
    scope: str


class DeletionExecutor(Protocol):
    """The purge (EF-BIB-052), which owns every permanent deletion."""

    def purge(self, candidates: tuple[DeletionCandidate, ...]) -> None: ...


@dataclass(frozen=True)
class DeletionConfirmation:
    principal: Principal


@dataclass(frozen=True)
class ReadoptLine:
    destination_id: str
    root_id: str
    rel_path: str
    file_hash: str
    export_item_id: str


@dataclass(frozen=True)
class RestoreReport:
    status: RestoreStatus
    new_files: tuple[str, ...]  # filed since the backup (mirror lines the manifest lacks)
    missing_files: tuple[str, ...]  # in the manifest, not on disk
    stale_intents: tuple[str, ...]  # came back with the backup: never replayed
    unknown_files: tuple[str, ...]  # in a managed series folder, in neither manifest nor mirror
    modified_files: tuple[str, ...]  # not the file their mirror line describes
    acquired_after_backup: tuple[str, ...]  # lines the restored database cannot place
    upstream_drift: tuple[UpstreamSeries, ...]
    upstream_proposal: str | None  # `remove_category` when no relevé covers the drift
    unverified_tombstone_lines: int = 0


@dataclass(frozen=True)
class AdoptionPreview:
    fingerprint: str
    report: RestoreReport
    readopt: tuple[ReadoptLine, ...]
    deletions: tuple[DeletionCandidate, ...]
    unresolved: tuple[tuple[str, str], ...]  # (rel path, reason)
    blocked_roots: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AdoptionResult:
    readopted: tuple[ReadoptLine, ...]
    pending_deletions: tuple[DeletionCandidate, ...]
    deleted: tuple[DeletionCandidate, ...]
    unresolved: tuple[tuple[str, str], ...]


# -- detection -------------------------------------------------------------------------------


class RestoreGuard:
    def __init__(self, engine: Engine, fsops: Fsops, *, clock: Clock, bus: EventBus) -> None:
        self._engine = engine
        self._fsops = fsops
        self._clock = clock
        self._bus = bus

    def assess(self) -> RestoreStatus:
        """Read only: every read-write root against its sentinel, as the database stands now."""
        with self._engine.connect() as connection:
            head = connection.execute(
                sa.select(
                    platform.instance.c.instance_id,
                    platform.instance.c.restore_epoch,
                    platform.instance.c.restore_generation,
                )
            ).first()
            paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
            if head is None or not paths:
                return RestoreStatus(0, ())
            loaded = load_roots(connection, PathMap.same_path(paths))
        findings = []
        for expected in loaded.expected:
            doc = self._fsops.sentinel_of(expected.path)
            state = judge_sentinel(doc, expected).state
            findings.append(
                RootFinding(
                    expected.root_id,
                    expected.path,
                    state,
                    expected.generation,
                    None if doc is None else doc.generation,
                    None if doc is None else doc.restore_epoch,
                )
            )
        return RestoreStatus(
            int(head.restore_epoch), tuple(findings), pending=int(head.restore_generation) > 0
        )

    def enter_restored_behind(self) -> RestoreStatus:
        """Start-up: if the database is behind, move the epoch counter (once per restore), say so
        once, and freeze the instance. Not behind: nothing changes."""
        status = self.assess()
        if not status.behind:
            return status
        epoch = status.epoch
        published: list[Any] = []
        if not status.pending:  # this restore has not been counted yet
            seen = max(
                (
                    f.sentinel_epoch
                    for f in status.findings
                    if f.sentinel_epoch is not None and f.sentinel_epoch <= MAX_RESTORE_EPOCH
                ),
                default=0,
            )
            top = max((f.sentinel_generation or 0 for f in status.behind_roots), default=0)
            with self._engine.begin() as connection:
                if status.epoch <= seen:
                    epoch = min(max(status.epoch, seen) + 1, MAX_RESTORE_EPOCH)
                # the freeze is written with the epoch: it survives a restart (until the adoption)
                connection.execute(
                    sa.update(platform.instance).values(
                        restore_epoch=epoch,
                        restore_epoch_at=self._clock.now(),
                        restore_generation=max(top, 1),
                    )
                )
                published.append(
                    self._bus.publish(
                        connection,
                        "instance.restored_behind",
                        {"epoch": epoch, "roots": len(status.behind_roots)},
                    )
                )
        self._bus.deliver(published)
        self._fsops.freeze()
        return RestoreStatus(epoch, status.findings, pending=True)

    def require_writable(self) -> None:
        if self._fsops.frozen:
            raise RestoredBehindError(
                "restored_behind: run `shelfhold reconcile --adopt-after-restore`"
            )


# -- the exit --------------------------------------------------------------------------------


@dataclass
class _Line:
    destination_id: str
    root_id: str
    rel_path: str
    local_path: str
    entry: dict[str, Any]
    header: dict[str, Any]
    outcome: LineOutcome = LineOutcome.ABSENT
    reason: str | None = None
    lineage: Lineage | None = None
    tombstone_scope: str | None = None
    on_disk: bool = False
    known: bool = False
    hash_matches: bool = False
    item_id: str | None = None


@dataclass
class _Survey:
    status: RestoreStatus
    loaded: LoadedRoots
    lines: list[_Line] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    # (destination, old path, new path): an intent applied after the backup moved the file
    relocations: list[tuple[str, str, str]] = field(default_factory=list)
    candidates: dict[tuple[str, str], DeletionCandidate] = field(default_factory=dict)
    blocked_units: dict[str, str] = field(default_factory=dict)  # unit id -> tombstone scope
    units: dict[str, str] = field(default_factory=dict)  # unit id -> edition id (in the database)
    variants: dict[str, str] = field(default_factory=dict)  # variant id -> unit id
    items: set[str] = field(default_factory=set)
    item_editions: dict[str, str] = field(default_factory=dict)
    editions: set[str] = field(default_factory=set)
    plugins: dict[str, set[str]] = field(default_factory=dict)  # source key -> plugin ids
    places: dict[str, tuple[dict[str, str], str, str, str, str]] = field(default_factory=dict)


def _bounded(value: object, limit: int = MAX_FIELD) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _chunks(items: Sequence[str]) -> list[Sequence[str]]:
    return [items[i : i + _CHUNK] for i in range(0, len(items), _CHUNK)]


class RestoreAdopter:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        screen: TombstoneScreen,
        *,
        clock: Clock,
        bus: EventBus,
        guard: RestoreGuard,
        upstream: UpstreamInventory | None = None,
        purger: DeletionExecutor | None = None,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._screen = screen
        self._clock = clock
        self._bus = bus
        self._guard = guard
        self._upstream = upstream
        self._purger = purger

    # -- the read-only report --------------------------------------------------------------

    def report(self) -> RestoreReport:
        """Inventory and differences, nothing written (neither the disk nor the database)."""
        return self._report(self._survey(screen=False))

    def _report(self, survey: _Survey) -> RestoreReport:
        drift, proposal = self._upstream_drift()
        return RestoreReport(
            status=survey.status,
            new_files=tuple(sorted(survey.new_files)),
            missing_files=tuple(sorted(survey.missing)),
            stale_intents=tuple(sorted(survey.stale)),
            unknown_files=tuple(sorted(survey.unknown)),
            modified_files=tuple(
                sorted(ln.rel_path for ln in survey.lines if ln.outcome is LineOutcome.MODIFIED)
            ),
            acquired_after_backup=tuple(
                sorted(
                    ln.rel_path for ln in survey.lines
                    if ln.outcome is LineOutcome.UNRESOLVED and ln.reason == "acquired_after_backup"
                )
            ),
            upstream_drift=drift,
            upstream_proposal=proposal,
        )  # fmt: skip

    def _upstream_drift(self) -> tuple[tuple[UpstreamSeries, ...], str | None]:
        if self._upstream is None:
            return (), None
        listed = list(self._upstream.shelfhold_series())
        with self._engine.connect() as connection:
            known = {
                (r.source_key, r.remote_id)
                for r in connection.execute(
                    sa.select(
                        acquisition.source_item.c.source_key, acquisition.source_item.c.remote_id
                    )
                )
            }
            covered = set(
                connection.execute(sa.select(acquisition.upstream_snapshot.c.series_ref)).scalars()
            )
        drift = tuple(
            sorted(
                (s for s in listed if (s.source_key, s.series_ref) not in known),
                key=lambda s: (s.source_key, s.series_ref),
            )
        )
        uncovered = any(s.series_ref not in covered for s in drift)
        return drift, ("remove_category" if drift and uncovered else None)

    # -- preview and adoption --------------------------------------------------------------

    def preview(self) -> AdoptionPreview:
        """What the adoption would do now, and the fingerprint that confirms exactly that. The
        tombstone journal is replayed first (I36): the preview already knows what it blocks."""
        return self._preview_of(self._survey(screen=True))

    def _thaw(self) -> None:
        """The end of `restored_behind`: the persisted freeze goes, then the instance thaws."""
        with self._engine.begin() as connection:
            connection.execute(sa.update(platform.instance).values(restore_generation=0))
        self._fsops.unfreeze()

    @staticmethod
    def _relocate(connection: Connection, survey: _Survey) -> None:
        """The manifest follows a file that an intent moved after the backup."""
        record, replica = export.export_record, storage.replica
        for destination_id, old, new in sorted(survey.relocations):
            connection.execute(
                sa.update(record)
                .where(record.c.shelf_destination_id == destination_id, record.c.rel_path == old)
                .values(rel_path=new)
            )
            connection.execute(
                sa.update(replica).where(replica.c.rel_path == old).values(rel_path=new)
            )

    def _preview_of(self, survey: _Survey) -> AdoptionPreview:
        readopt = self._readopt(survey)
        deletions = tuple(survey.candidates[k] for k in sorted(survey.candidates))
        unresolved = tuple(
            sorted(
                (ln.rel_path, ln.reason or "")
                for ln in survey.lines
                if ln.outcome is LineOutcome.UNRESOLVED
            )
        )
        blocked = {
            f.root_id: reason
            for f in survey.status.findings
            if (reason := self._screen.root_blocked(f.root_id)) is not None
        }
        report = replace(self._report(survey), unverified_tombstone_lines=self._screen.unverified())
        document = {
            "epoch": survey.status.epoch,
            "roots": sorted(
                (f.root_id, f.db_generation, f.sentinel_generation, f.sentinel_epoch)
                for f in survey.status.findings
            ),
            "readopt": sorted(
                (r.destination_id, r.rel_path, r.file_hash, r.export_item_id) for r in readopt
            ),
            "deletions": [(d.destination_id, d.rel_path, d.scope) for d in deletions],
            "unresolved": list(unresolved),
            "blocked": sorted(blocked.items()),
            "unknown": list(report.unknown_files),
            "modified": list(report.modified_files),
            "missing": list(report.missing_files),
            "relocations": sorted(survey.relocations),
            "decisions": [list(step) for step in self._plan_decisions(survey)],
            "blocked_units": sorted(survey.blocked_units.items()),
        }
        return AdoptionPreview(
            fingerprint_of(document), report, readopt, deletions, unresolved, blocked
        )

    @staticmethod
    def _readopt(survey: _Survey) -> tuple[ReadoptLine, ...]:
        return tuple(
            ReadoptLine(
                ln.destination_id, ln.root_id, ln.rel_path, str(ln.entry["file_hash"]),
                ln.lineage.export_item_id,
            )
            for ln in survey.lines
            if ln.outcome is LineOutcome.READOPT and ln.lineage is not None
        )  # fmt: skip

    def adopt(
        self, *, confirm: str, deletion: DeletionConfirmation | None = None
    ) -> AdoptionResult:
        """Only for the fingerprint of the preview, and never deleting by itself."""
        if deletion is not None:
            verdict = authorize_reserved(deletion.principal, _DELETION, now=self._clock.now())
            if not isinstance(verdict, Allowed):
                raise DeletionRefusedError(verdict)
        survey = self._survey(screen=True)
        preview = self._preview_of(survey)
        for root_id, reason in sorted(preview.blocked_roots.items()):
            raise AdoptionBlockedError(root_id, reason)
        if preview.fingerprint != confirm:
            raise PreviewStaleError("the instance changed since the preview: look again")
        if deletion is not None and preview.deletions and self._purger is None:
            raise DeletionUnavailableError("no purge executor: nothing is deleted")
        now = self._clock.now()
        new_generation: dict[str, int] = {}
        with self._engine.begin() as connection:
            for line in sorted(survey.lines, key=lambda ln: (ln.destination_id, ln.rel_path)):
                if line.outcome is LineOutcome.READOPT:
                    self._insert(connection, line, now, survey)
            self._relocate(connection, survey)
            self._decisions(connection, survey, now)
            self._block_units(connection, survey, now)
            if deletion is not None and preview.deletions:
                connection.execute(
                    sa.insert(identity.audit).values(
                        id=new_ulid(), actor_id=deletion.principal.actor_id,
                        action="reconcile.confirm_deletion",
                        target=f"candidates:{len(preview.deletions)}",
                        session_id=deletion.principal.session_id,
                        auth_time=deletion.principal.auth_time,
                    )
                )  # fmt: skip
            for finding in survey.status.findings:
                moved = max(finding.db_generation, finding.sentinel_generation or 0) + 1
                new_generation[finding.root_id] = moved
                connection.execute(
                    sa.update(platform.physical_root)
                    .where(platform.physical_root.c.root_id == finding.root_id)
                    .values(generation=moved)
                )  # fmt: skip
        for finding in survey.status.findings:
            if finding.sentinel_generation is None:
                continue  # a root that is not mounted: nothing to re-stamp
            try:
                self._fsops.restamp_after_restore(
                    finding.path, generation=new_generation[finding.root_id],
                    restore_epoch=survey.status.epoch,
                )  # fmt: skip
            except FsopsError:
                log.warning("restamp_failed", root=finding.root_id)  # the next start retries
        chosen = preview.deletions
        deleted: tuple[DeletionCandidate, ...] = ()
        if deletion is not None and chosen and self._purger is not None:
            self._purger.purge(chosen)
            deleted = chosen
        if not chosen or deleted:  # a deletion still waiting keeps the instance read only
            self._thaw()
        return AdoptionResult(
            self._readopt(survey), () if deleted else chosen, deleted, preview.unresolved
        )

    # -- looking at the disk and the mirrors -----------------------------------------------

    def _survey(self, *, screen: bool) -> _Survey:
        status = self._guard.assess()
        with self._engine.connect() as connection:
            paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
            loaded = load_roots(connection, PathMap.same_path(paths))
        mounted = {r.root_id: self._fsops.sentinel_of(r.path) is not None for r in loaded.roots
                   if r.mode is PathMode.RW}  # fmt: skip
        if screen:
            self._screen.replay(
                [(r.root_id, r.path, mounted[r.root_id], r.predecessors)
                 for r in loaded.roots if r.mode is PathMode.RW]
            )  # fmt: skip
        survey = _Survey(status, loaded)
        with self._engine.connect() as connection:
            self._stale_intents(connection, survey)
            contexts = self._contexts(connection, loaded)
            manifest = self._manifest(connection, contexts)
            for root in loaded.expected:
                if mounted.get(root.root_id):
                    self._read_mirrors(root, contexts, manifest, survey)
            self._prefetch(connection, survey)
            self._missing_and_unknown(contexts, manifest, survey)
            if screen:
                self._screen_lines(survey, contexts)
                self._screen_database(connection, survey, contexts)
        for line in survey.lines:
            if line.outcome is LineOutcome.UNRESOLVED and line.reason in _FIXED_REASONS:
                continue
            facts = LineFacts(
                on_disk=line.on_disk, hash_matches=line.hash_matches,
                known_in_manifest=line.known, tombstoned=line.tombstone_scope is not None,
                lineage_granted=line.lineage is not None,
                unresolved_reason=self._reason(line, survey),
            )  # fmt: skip
            line.outcome, line.reason = judge_line(facts)
            if line.outcome is LineOutcome.TOMBSTONED and line.on_disk:
                survey.candidates.setdefault(
                    (line.destination_id, line.rel_path),
                    DeletionCandidate(
                        line.destination_id, line.root_id, line.rel_path, line.local_path,
                        line.tombstone_scope or "variant",
                    ),
                )  # fmt: skip
            if (
                line.outcome in (LineOutcome.READOPT, LineOutcome.UNRESOLVED) and not line.known
            ) or line.outcome is LineOutcome.MODIFIED:
                survey.new_files.append(line.rel_path)
        return survey

    @staticmethod
    def _stale_intents(connection: Connection, survey: _Survey) -> None:
        table = coherence.fs_intent
        survey.stale = list(
            connection.execute(
                sa.select(table.c.id).where(
                    table.c.state.in_(("pending", "applying", "stale_from_backup")),
                    table.c.epoch < survey.status.epoch,
                )
            ).scalars()
        )

    @staticmethod
    def _contexts(connection: Connection, loaded: LoadedRoots) -> dict[str, DestinationContext]:
        found: dict[str, DestinationContext] = {}
        for destination_id in connection.execute(
            sa.select(export.shelf_destination.c.id)
        ).scalars():
            try:
                ctx = load_context(connection, destination_id)
            except (sa.exc.NoResultFound, ValueError, KeyError):
                continue
            if ctx.transport == "tree" and ctx.root_local is not None:
                found[destination_id] = ctx
        return found

    @staticmethod
    def _manifest(
        connection: Connection, contexts: Mapping[str, DestinationContext]
    ) -> dict[str, dict[str, tuple[str, str, str | None]]]:
        """destination -> rel_path -> (export item id, state, file hash), whatever the state."""
        record = export.export_record
        found: dict[str, dict[str, tuple[str, str, str | None]]] = defaultdict(dict)
        for row in connection.execute(
            sa.select(record.c.shelf_destination_id, record.c.rel_path, record.c.export_item_id,
                      record.c.state, record.c.file_hash).where(record.c.rel_path.is_not(None))
        ):  # fmt: skip
            if row.shelf_destination_id in contexts:
                found[row.shelf_destination_id][row.rel_path] = (
                    row.export_item_id, row.state, row.file_hash,
                )  # fmt: skip
        return found

    def _read_mirrors(
        self,
        root: ExpectedRoot,
        contexts: Mapping[str, DestinationContext],
        manifest: Mapping[str, Mapping[str, tuple[str, str, str | None]]],
        survey: _Survey,
    ) -> None:
        try:
            names = self._fsops.list_mirror(root.path)
        except FsopsError:
            return
        seen: set[tuple[str, str]] = set()
        for name in names:
            destination_id = name.split("/", 1)[0]
            ctx = contexts.get(destination_id)
            if ctx is None or ctx.root_local is None:
                continue
            if within(clean_absolute(ctx.root_local), root.path) is None:
                continue  # this destination's tree lives on another root
            try:
                raw = self._fsops.read_mirror(root.path, name)
                document = MirrorDocument.parse(raw) if raw is not None else None
            except (MirrorFormatError, ValueError, FsopsError, RecursionError):
                continue  # a damaged mirror grants nothing; the file is left as it is
            if document is None or document.read_only:
                continue
            by_item = {item for item, _, _ in manifest.get(destination_id, {}).values()}
            for entry in document.entries:
                rel = safe_rel_path(entry.get("rel_path"))
                if rel is None or (destination_id, rel) in seen:
                    continue
                seen.add((destination_id, rel))
                line = _Line(destination_id, root.root_id, rel, f"{ctx.root_local}/{rel}", entry,
                             document.header)  # fmt: skip
                item = entry.get("export_item")
                item_id = item.get("id") if isinstance(item, dict) else None
                line.known = rel in manifest.get(destination_id, {}) or item_id in by_item
                line.item_id = item_id if isinstance(item_id, str) else None
                try:
                    found = self._fsops.observe(line.local_path)
                    line.on_disk = found is not None and found[0] == "file"
                    if line.on_disk and not line.known:
                        disk = self._fsops.sha256_of(line.local_path)
                        line.hash_matches = entry.get("file_hash") == disk
                        if line.hash_matches:
                            line.lineage = grant_lineage(entry, disk_file_hash=disk)
                except FsopsError:  # a symbolic link in the way, a path out of the root
                    line.on_disk = False
                    line.outcome, line.reason = LineOutcome.UNRESOLVED, "path_escape"
                survey.lines.append(line)

    def _prefetch(self, connection: Connection, survey: _Survey) -> None:
        units: set[str] = set()
        variants: set[str] = set()
        items: set[str] = set()
        editions: set[str] = set()
        for line in survey.lines:
            cover = line.entry.get("coverage")
            for covered in cover if isinstance(cover, list) else []:
                if isinstance(covered, dict):
                    units.update(u for u in (covered.get("unit"),) if isinstance(u, str))
                    variants.update(v for v in (covered.get("variant"),) if isinstance(v, str))
            item = line.entry.get("export_item")
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                items.add(item["id"])
            for ref in (line.entry.get("edition"), line.header.get("edition")):
                if isinstance(ref, str):
                    editions.add(ref)
        edition = catalog.edition
        for chunk in _chunks(sorted(units)):
            for row in connection.execute(
                sa.select(
                    catalog.unit.c.id, catalog.unit.c.edition_id, catalog.unit.c.track,
                    catalog.unit.c.number_segment, catalog.unit.c.number_raw, catalog.unit.c.part,
                    *(edition.c[name] for name in EDITION_KEY_COLUMNS),
                )
                .select_from(catalog.unit.join(edition, edition.c.id == catalog.unit.c.edition_id))
                .where(catalog.unit.c.id.in_(chunk))
            ):  # fmt: skip
                survey.units[row.id] = row.edition_id
                survey.places[row.id] = (
                    {k: str(getattr(row, k)) for k in EDITION_KEY_COLUMNS},
                    row.track, row.number_segment, row.number_raw, row.part,
                )  # fmt: skip
        keys = sorted(
            {
                c["source_key"]
                for ln in survey.lines
                for c in (ln.entry.get("coverage") or [])
                if isinstance(c, dict) and isinstance(c.get("source_key"), str)
            }
        )
        for chunk in _chunks(keys):
            for row in connection.execute(
                sa.select(acquisition.source_item.c.source_key, acquisition.source_item.c.plugin_id)
                .where(acquisition.source_item.c.source_key.in_(chunk))
                .distinct()
            ):
                survey.plugins.setdefault(row.source_key, set()).add(row.plugin_id)
        for chunk in _chunks(sorted(variants)):
            for row in connection.execute(
                sa.select(catalog.unit_variant.c.id, catalog.unit_variant.c.unit_id).where(
                    catalog.unit_variant.c.id.in_(chunk)
                )
            ):
                survey.variants[row.id] = row.unit_id
        for chunk in _chunks(sorted(items)):
            for row in connection.execute(
                sa.select(export.export_item.c.id, export.export_item.c.edition_id).where(
                    export.export_item.c.id.in_(chunk)
                )
            ):
                survey.items.add(row.id)
                survey.item_editions[row.id] = row.edition_id
        for chunk in _chunks(sorted(editions)):
            survey.editions.update(
                connection.execute(
                    sa.select(catalog.edition.c.id).where(catalog.edition.c.id.in_(chunk))
                ).scalars()
            )
        for chunk in _chunks(sorted(set(survey.units.values()))):
            survey.editions.update(
                connection.execute(
                    sa.select(catalog.edition.c.id).where(catalog.edition.c.id.in_(chunk))
                ).scalars()
            )

    @staticmethod
    def _reason(line: _Line, survey: _Survey) -> str | None:
        """Why the restored database cannot place a line that the mirror describes, if it cannot."""
        lineage = line.lineage
        if lineage is None:
            return None
        item = line.entry.get("export_item")
        kind = item.get("kind", "unit") if isinstance(item, dict) else "unit"
        frozen = item.get("frozen_attrs", {}) if isinstance(item, dict) else {}
        if kind not in KIND_VALUES or not isinstance(frozen, dict):
            return "lineage"
        units = [c.get("unit") for c in lineage.coverage]
        try:
            check_coverage(kind, [str(u) for u in units])
        except CoverageError:
            return "lineage"
        if any(u not in survey.units for u in units):
            return "acquired_after_backup"
        editions = {survey.units[str(u)] for u in units}
        if len(editions) != 1 or (lineage.edition_id and lineage.edition_id not in editions):
            return "unit_conflict"
        for covered in lineage.coverage:
            variant = covered.get("variant")
            if variant is None:
                continue
            if variant not in survey.variants:
                return "acquired_after_backup"
            if survey.variants[variant] != covered.get("unit"):
                return "unit_conflict"
        if lineage.export_item_id in survey.items:
            row = survey.item_editions.get(lineage.export_item_id)
            if row is not None and row not in editions:
                return "item_conflict"
        return None

    def _missing_and_unknown(
        self,
        contexts: Mapping[str, DestinationContext],
        manifest: Mapping[str, Mapping[str, tuple[str, str, str | None]]],
        survey: _Survey,
    ) -> None:
        live = {"written", "scanned", "resolved", "adopted"}
        mirrored: dict[str, set[str]] = defaultdict(set)
        for line in survey.lines:
            mirrored[line.destination_id].add(line.rel_path)
        for destination_id, ctx in contexts.items():
            records = manifest.get(destination_id, {})
            root = ctx.root_local
            if root is None:
                continue
            gone: list[tuple[str, str, str | None]] = []
            for rel, (item, state, file_hash) in records.items():
                if state not in live:
                    continue
                try:
                    absent = self._fsops.observe(f"{root}/{rel}") is None
                except FsopsError:
                    continue  # a link in the way: neither missing nor known, left as it is
                if absent:
                    gone.append((rel, item, file_hash))
            known = set(records) | mirrored[destination_id]
            unknown: list[str] = []
            folders = sorted({r.split("/", 1)[0] for r in known if "/" in r})
            for folder in folders:
                for rel in self._files_under(f"{root}/{folder}", folder):
                    if rel not in known:
                        unknown.append(rel)
            moved = self._pair_moved(destination_id, root, gone, unknown, records, survey)
            survey.missing.extend(rel for rel, _, _ in gone if rel not in moved)
            survey.unknown.extend(rel for rel in unknown if rel not in moved.values())

    def _pair_moved(
        self,
        destination_id: str,
        root: str,
        gone: list[tuple[str, str, str | None]],
        unknown: list[str],
        records: Mapping[str, tuple[str, str, str | None]],
        survey: _Survey,
    ) -> dict[str, str]:
        """An intent applied after the backup moved a file: the manifest says `old`, the disk has
        `new`. Paired by export item (the mirror line) or by file hash, and only when the file on
        disk is exactly the one the manifest recorded. Returns old path -> new path."""
        if not gone:
            return {}
        found: dict[str, str] = {}
        taken: set[str] = set()
        mirrored = {
            ln.item_id: ln
            for ln in survey.lines
            if ln.destination_id == destination_id
            and ln.on_disk
            and ln.item_id is not None
            and ln.rel_path not in records
        }
        by_hash: dict[str, list[str]] = defaultdict(list)
        for rel in unknown:
            try:
                digest = self._fsops.sha256_of(f"{root}/{rel}")
            except (FsopsError, OSError):
                continue
            by_hash[digest].append(rel)
        for rel, item, file_hash in gone:
            if file_hash is None:
                continue
            candidates: list[str] = []
            line = mirrored.get(item)
            if line is not None:
                try:
                    if self._fsops.sha256_of(line.local_path) == file_hash:
                        candidates.append(line.rel_path)
                except (FsopsError, OSError):
                    pass
            if not candidates:
                candidates = [r for r in by_hash.get(file_hash, []) if r not in taken]
            if len(candidates) == 1 and candidates[0] not in taken:
                found[rel] = candidates[0]
                taken.add(candidates[0])
                survey.relocations.append((destination_id, rel, candidates[0]))
        return found

    def _files_under(self, local: str, rel: str, depth: int = 0) -> list[str]:
        found: list[str] = []
        try:
            entries = self._fsops.list_dir(local)
        except OSError:
            return found
        for name, kind in sorted(entries.items()):
            if name in RESERVED_NAMES:
                continue
            if kind == "dir" and depth < MAX_DEPTH:
                found += self._files_under(f"{local}/{name}", f"{rel}/{name}", depth + 1)
            else:
                found.append(f"{rel}/{name}")
        return found

    # -- tombstones ------------------------------------------------------------------------

    def _screen_lines(self, survey: _Survey, contexts: Mapping[str, DestinationContext]) -> None:
        for line in survey.lines:
            if not line.on_disk:
                continue
            scope = self._line_scope(line, survey)
            if scope is not None:
                line.tombstone_scope = scope

    def _line_scope(self, line: _Line, survey: _Survey) -> str | None:
        """Which tombstone scope covers this file, in the layout the purge records (L34): a
        Variant is `(plugin id, source key, remote unit id)`, a place is the Edition key and the
        Unit's place, a series is `(plugin id, source key, remote series id)`."""
        entry = line.entry
        hashes = self._hashes(entry)
        binding = line.header.get("binding")
        bound = binding if isinstance(binding, dict) else {}
        cover = entry.get("coverage")
        for covered in cover if isinstance(cover, list) else []:
            if not isinstance(covered, dict):
                continue
            source_key = _bounded(covered.get("source_key"))
            remote = _bounded(covered.get("remote_unit_id"))
            if source_key is not None and remote is not None:
                plugins = set(survey.plugins.get(source_key, ()))
                if _bounded(bound.get("plugin_id")) and bound.get("source_key") == source_key:
                    plugins.add(str(bound["plugin_id"]))
                if any(
                    self._screen.variant_blocked(line.root_id, p, source_key, remote, hashes)
                    for p in sorted(plugins)
                ):
                    return "variant"
            place = survey.places.get(str(covered.get("unit")))
            if place is not None and self._screen.place_blocked(line.root_id, *place):
                return "place"
        if hashes and self._screen.content_blocked(line.root_id, hashes):
            return "variant"
        plugin, key, ref = (
            _bounded(bound.get("plugin_id")),
            _bounded(bound.get("source_key")),
            _bounded(bound.get("series_ref")),
        )
        if plugin and key and ref and self._screen.series_blocked(line.root_id, plugin, key, ref):
            return "series_binding"
        return None

    @staticmethod
    def _hashes(entry: Mapping[str, Any]) -> list[tuple[str, str]]:
        by, value = entry.get("content_hash_by"), entry.get("content_hash")
        if isinstance(by, str) and by and isinstance(value, str) and _SHA.match(value):
            return [(by, value)]
        return []

    def _screen_database(
        self, connection: Connection, survey: _Survey, contexts: Mapping[str, DestinationContext]
    ) -> None:
        """The restored database still holds what was purged after the backup: its Units are
        blocked from any new request, and the files it exported are proposed for deletion."""
        unit, variant, edition = catalog.unit, catalog.unit_variant, catalog.edition
        binding, item, stored = acquisition.source_binding, acquisition.source_item, storage.content
        rows = connection.execute(
            sa.select(
                unit.c.id, unit.c.track, unit.c.number_segment, unit.c.number_raw, unit.c.part,
                edition.c.work_id, edition.c.lang, edition.c.translation_type, edition.c.media,
                edition.c.publisher, edition.c.text_version, edition.c.performance,
                edition.c.packaging, variant.c.remote_unit_id, item.c.plugin_id, item.c.source_key,
                item.c.remote_id, stored.c.content_hash, stored.c.content_hash_by,
            )
            .select_from(
                unit.join(edition, edition.c.id == unit.c.edition_id)
                .outerjoin(variant, variant.c.unit_id == unit.c.id)
                .outerjoin(binding, binding.c.id == variant.c.source_binding_id)
                .outerjoin(item, item.c.id == binding.c.source_item_id)
                .outerjoin(stored, stored.c.variant_id == variant.c.id)
            )
        )  # fmt: skip
        scopes: dict[str, str] = {}
        for row in rows:
            if row.id in scopes:
                continue
            hashes = (
                [(row.content_hash_by, row.content_hash)]
                if row.content_hash and row.content_hash_by
                else []
            )
            if (
                row.remote_unit_id
                and row.source_key
                and self._screen.variant_blocked(
                    None, row.plugin_id, row.source_key, row.remote_unit_id, hashes
                )
            ):
                scopes[row.id] = "variant"
            elif self._screen.place_blocked(
                None,
                {
                    "work_id": row.work_id, "lang": row.lang,
                    "translation_type": row.translation_type, "media": row.media,
                    "publisher": row.publisher, "text_version": row.text_version,
                    "performance": row.performance, "packaging": row.packaging,
                },
                row.track, row.number_segment, row.number_raw, row.part,
            ):  # fmt: skip
                scopes[row.id] = "place"
            elif (
                row.source_key
                and row.remote_id
                and self._screen.series_blocked(None, row.plugin_id, row.source_key, row.remote_id)
            ):
                scopes[row.id] = "series_binding"
        survey.blocked_units = dict(scopes)
        if not scopes:
            return
        record, coverage = export.export_record, export.unit_coverage
        for chunk in _chunks(sorted(scopes)):
            for found in connection.execute(
                sa.select(record.c.shelf_destination_id, record.c.rel_path, coverage.c.unit_id)
                .select_from(
                    record.join(
                        coverage,
                        sa.and_(
                            coverage.c.carrier_type == "export_item",
                            coverage.c.carrier_id == record.c.export_item_id,
                        ),
                    )
                )
                .where(coverage.c.unit_id.in_(chunk), record.c.rel_path.is_not(None),
                       record.c.state.in_(("written", "scanned", "resolved", "adopted")))
            ):  # fmt: skip
                ctx = contexts.get(found.shelf_destination_id)
                if ctx is None or ctx.root_local is None:
                    continue
                local = f"{ctx.root_local}/{found.rel_path}"
                try:
                    seen = self._fsops.observe(local)
                except FsopsError:
                    continue  # never proposed for deletion through a link
                root_id = survey.loaded.root_id_for(local)
                if seen is None or seen[0] != "file" or root_id is None:
                    continue
                survey.candidates.setdefault(
                    (found.shelf_destination_id, found.rel_path),
                    DeletionCandidate(
                        found.shelf_destination_id, root_id, found.rel_path, local,
                        scopes[found.unit_id],
                    ),
                )  # fmt: skip

    # -- writing the database --------------------------------------------------------------

    def _insert(self, connection: Connection, line: _Line, now: datetime, survey: _Survey) -> None:
        lineage = line.lineage
        if lineage is None:  # READOPT implies a granted lineage
            raise LookupError(line.rel_path)
        entry = line.entry
        item = entry["export_item"]
        kind = item.get("kind", "unit")
        frozen = item.get("frozen_attrs") if isinstance(item.get("frozen_attrs"), dict) else {}
        item_id = lineage.export_item_id
        first = lineage.coverage[0]
        edition_id = survey.units[str(first["unit"])]
        if item_id not in survey.items:
            content_id = None
            if len(lineage.coverage) == 1 and first.get("variant"):
                content_id = connection.execute(
                    sa.select(storage.content.c.id)
                    .where(storage.content.c.variant_id == first["variant"])
                    .limit(1)
                ).scalar()
            connection.execute(
                sa.insert(export.export_item).values(
                    id=item_id, edition_id=edition_id, kind=kind, frozen_attrs=frozen,
                    content_id=content_id,
                )
            )  # fmt: skip
            for position, covered in enumerate(lineage.coverage):
                connection.execute(
                    sa.insert(export.unit_coverage).values(
                        carrier_type="export_item", carrier_id=item_id, unit_id=covered["unit"],
                        position=position, start_locator=covered.get("start"),
                        end_locator=covered.get("end"), extent="full", origin="manual",
                    )
                )  # fmt: skip
        try:
            found = self._fsops.observe(line.local_path)
        except FsopsError as error:
            raise PreviewStaleError(f"{line.rel_path}: moved since the preview") from error
        if found is None:
            raise PreviewStaleError(f"{line.rel_path}: gone since the preview")
        identity_ = found[1]
        written = self._moment(entry.get("written_at")) or now
        file_hash = str(entry["file_hash"])
        content_hash = lineage.content_hash if _SHA.match(str(lineage.content_hash)) else None
        connection.execute(
            sa.insert(export.export_record).values(
                shelf_destination_id=line.destination_id, export_item_id=item_id,
                locator_type="path", rel_path=line.rel_path,
                template_ref=(
                    lineage.template_ref if isinstance(lineage.template_ref, str) else None
                ),
                size=identity_.size, mtime_ns=identity_.mtime_ns, content_hash=content_hash,
                file_hash=file_hash,
                written_by_shelfhold_at=None if kind == "adopted" else written,
                exported_at=written, state="adopted" if kind == "adopted" else "written",
            )
        )  # fmt: skip
        record_destination_copy(
            connection, unit_id=str(first["unit"]), export_item_id=item_id,
            content_hash=content_hash or file_hash, file_hash=file_hash,
            physical_root_id=line.root_id, rel_path=line.rel_path, size=identity_.size,
            mtime_ns=identity_.mtime_ns, ino=identity_.ino,
            dev=self._fsops.device_of(line.local_path), verified_at=now,
        )  # fmt: skip

    @staticmethod
    def _moment(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
        return moment if moment.tzinfo is not None else None

    @staticmethod
    def _plan_decisions(survey: _Survey) -> list[tuple[Any, ...]]:
        """What the mirror's decisions would apply: "stop following" (restrictive, comes back as it
        was said), an age (only ever a suggestion, GAP-9-10: no rating, no shelf, no visibility is
        granted by a file) and the follow-from point. Pure: the fingerprint covers all of it."""
        plan: set[tuple[Any, ...]] = set()
        for line in survey.lines:
            if line.outcome is LineOutcome.TOMBSTONED:
                continue
            cover = line.entry.get("coverage")
            for covered in cover if isinstance(cover, list) else []:
                if (
                    isinstance(covered, dict) and covered.get("follow_suppressed") is True
                    and covered.get("unit") in survey.units
                ):  # fmt: skip
                    plan.add(("follow_suppressed", covered["unit"]))
            age = line.entry.get("age_classified")
            edition = line.entry.get("edition")
            if (
                isinstance(age, dict) and isinstance(edition, str) and edition in survey.editions
                and isinstance(age.get("min_age"), int) and not isinstance(age.get("min_age"), bool)
                and 0 <= age["min_age"] <= 99
            ):  # fmt: skip
                plan.add(("age_suggestion", edition, age["min_age"], age.get("x") is True))
            follow = line.header.get("follow_from")
            head = line.header.get("edition")
            if isinstance(follow, dict) and isinstance(head, str):
                number, segment = follow.get("number"), follow.get("segment", "")
                if (
                    isinstance(number, str) and _NUMBER.match(number)
                    and isinstance(segment, str) and len(segment) <= 64
                ):  # fmt: skip
                    plan.add(("follow_from", head, number, segment))
        return sorted(plan, key=repr)

    def _decisions(self, connection: Connection, survey: _Survey, now: datetime) -> None:
        unit, suggestion, follow = catalog.unit, catalog.age_suggestion, acquisition.series_follow
        for step in self._plan_decisions(survey):
            if step[0] == "follow_suppressed":
                connection.execute(
                    sa.update(unit)
                    .where(unit.c.id == step[1], sa.not_(unit.c.follow_suppressed))
                    .values(follow_suppressed=True, follow_suppressed_at=now)
                )
            elif step[0] == "age_suggestion":
                connection.execute(
                    sa.insert(suggestion)
                    .values(
                        id=new_ulid(), entity_type="edition", entity_id=step[1], value=step[2],
                        x=step[3], provenance="import", seen_at=now, state="open",
                    )
                    .prefix_with("OR IGNORE")
                )  # fmt: skip
            else:
                connection.execute(
                    sa.update(follow)
                    .where(follow.c.edition_id == step[1], follow.c.follow_from_number.is_(None))
                    .values(follow_from_segment=step[3], follow_from_number=step[2])
                )

    @staticmethod
    def _block_units(connection: Connection, survey: _Survey, now: datetime) -> None:
        """What a tombstone covers is not requested again, whatever the follow cycle says."""
        unit = catalog.unit
        for unit_id in sorted(survey.blocked_units):
            connection.execute(
                sa.update(unit)
                .where(unit.c.id == unit_id, sa.not_(unit.c.follow_suppressed))
                .values(follow_suppressed=True, follow_suppressed_at=now)
            )
