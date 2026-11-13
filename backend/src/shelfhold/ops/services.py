"""The services behind the exit commands of EF-INT-023, over the services that already exist.

Nothing here writes SQL to leave a state when a service for it exists: a pause of a series is lifted
by the `Reconciler`, a permission pause by the `PermissionPreflight`, a target by the Kavita
client, a secret by the headless facade, the lost keyring by the tombstone service. What is left
(`missing resolve`, `jobs retry`, `edition set-lang`) is a single, guarded update of the row
that holds the wait. The HTTP routes, the socket and the CLI all call this class; the reserved
classification is theirs (the route or the socket decides before calling).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.acquisition.application.requests import RequestService
from shelfhold.acquisition.application.unit_follow import suppress_follow
from shelfhold.acquisition.public import FOLLOW_JOB_KIND, make_all_due
from shelfhold.catalog.lang import LANG_NORMALIZER_ID, LanguageError, edition_key_lang
from shelfhold.events.bus import EventBus
from shelfhold.export.application import root_capabilities, waiting_marks
from shelfhold.export.application.export_items import SeriesCollisionError, ensure_export_series
from shelfhold.export.application.foreign_names import load_foreign_names
from shelfhold.export.application.preflight import PermissionPreflight
from shelfhold.export.application.reconcile import Reconciler
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.application.target_state import load_health
from shelfhold.export.domain.naming import normalize_series_name, series_name_refusal
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.export.domain.planner import SeriesRef
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import catalog, coherence, export, platform
from shelfhold.jobs.control import QueueControl
from shelfhold.jobs.window import DEFAULT_WINDOW, MaintenanceWindow
from shelfhold.ops.acquire import AcquireOps
from shelfhold.ops.attach import AttachOps
from shelfhold.ops.backfill import BackfillOps
from shelfhold.ops.content import ContentOps
from shelfhold.ops.contributors import ContributorOps
from shelfhold.ops.dedup import DedupOps
from shelfhold.ops.edition_merge import EditionMergeOps
from shelfhold.ops.editions import EditionOps
from shelfhold.ops.errors import (
    CodedRefusal,
    ExitUnavailableError,
    InvalidChoiceError,
    LanguageRefusedError,
    NotWaitingError,
    StillWaitingError,
    UnknownSubjectError,
)
from shelfhold.ops.export_all import ExportAllOps
from shelfhold.ops.export_item import ExportItemOps
from shelfhold.ops.follows import FollowOps
from shelfhold.ops.import_folder import ImportOps
from shelfhold.ops.intents import IntentOps
from shelfhold.ops.kavita_accounts import KavitaAccountsOps
from shelfhold.ops.library import LibraryOps
from shelfhold.ops.maintenance import MaintenanceOps
from shelfhold.ops.meta import MetaOps
from shelfhold.ops.normalize import NormalizeOps
from shelfhold.ops.ports import (
    AdoptionExit,
    CredentialsExit,
    HeadlessOps,
    KeyringExit,
    PausedQueue,
    RestoreExit,
    TargetOps,
    WaitingItem,
)
from shelfhold.ops.rating import RatingOps
from shelfhold.ops.resplit import ResplitOps
from shelfhold.ops.revisions import RevisionOps
from shelfhold.ops.roots import RootOps
from shelfhold.ops.sheet import SheetOps
from shelfhold.ops.source_migration import SourceMigrationOps
from shelfhold.ops.status import (
    UNKNOWN_LANGUAGE,
    candidate_report,
    collect,
    dress,
    jurisdiction_report,
    kept_in_service_report,
    known_bad_report,
    koreader_reset_report,
    queues_of,
    rights_report,
    service_url_report,
    stale_report,
    tombstoned_report,
    unverifiable_download_dirs,
    wal_pending_report,
)
from shelfhold.ops.wizard import WizardOps
from shelfhold.ops.work_merge import WorkMergeOps

__all__ = ["JobRow", "OpsServices", "RecheckOutcome", "StatusReport", "user_of"]

DEAD = ("dead_letter", "failed")
NOT_INDEXED = "target.not_indexed"
MAX_SERIES_NAME = 200
KOREADER_RESET_WINDOW = timedelta(days=7)  # how far back `status` lists the upgrades
ULID_PATTERN = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
WINDOW_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # how the scheduler stores the instants of a window (UTC)


def user_of(principal: Principal) -> str | None:
    """The user a provenance column may name (a foreign key to `user`): the account behind a
    session or a key; the host CLI is no account (its uid is in the audit log of the socket)."""
    return principal.user_id if ULID_PATTERN.fullmatch(principal.user_id) else None


@dataclass(frozen=True)
class StatusReport:
    waiting: list[WaitingItem]
    queues: list[PausedQueue]
    stale_series: list[dict[str, object]] = field(default_factory=list)
    tombstoned_upstream: list[dict[str, object]] = field(default_factory=list)
    capabilities: list[dict[str, object]] = field(default_factory=list)
    known_bad: dict[str, object] = field(default_factory=dict)
    service_urls: list[dict[str, object]] = field(default_factory=list)
    unverifiable_dirs: list[dict[str, object]] = field(default_factory=list)
    content_error_candidates: list[dict[str, object]] = field(default_factory=list)
    jurisdiction: dict[str, object] | None = None  # EF-SRC-024: the country, confirmed or not
    rights_warnings: list[dict[str, object]] = field(default_factory=list)
    kept_in_service: list[dict[str, object]] = field(default_factory=list)
    wal_pending: list[dict[str, object]] = field(default_factory=list)
    koreader_resets: list[dict[str, object]] = field(default_factory=list)  # EF-EXP-016, D21

    def as_dict(self) -> dict[str, object]:
        return {
            "waiting": [vars(i) | {} for i in self.waiting],
            "queues": [vars(q) | {} for q in self.queues],
            "stale_series": self.stale_series,
            "tombstoned_upstream": self.tombstoned_upstream,
            "capabilities": self.capabilities,
            "known_bad": self.known_bad,
            "service_urls": self.service_urls,
            "unverifiable_dirs": self.unverifiable_dirs,
            "content_error_candidates": self.content_error_candidates,
            "jurisdiction": self.jurisdiction,
            "rights_warnings": self.rights_warnings,
            "kept_in_service": self.kept_in_service,
            "wal_pending": self.wal_pending,
            "koreader_resets": self.koreader_resets,
        }


@dataclass(frozen=True)
class JobRow:
    id: str
    kind: str
    status: str
    error_code: str | None
    last_error: str | None
    attempts: int
    created_at: datetime | None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: float | None = None
    request_id: str | None = None
    priority_class: str = "new"
    source_key: str | None = None


@dataclass(frozen=True)
class RecheckOutcome:
    cleared: tuple[str, ...]  # export series that can be written again
    denied: tuple[str, ...]  # export series still waiting for the rights


def _job_row(r: sa.Row[tuple[object, ...]]) -> JobRow:
    return JobRow(
        r.id, r.kind, r.status, r.error_code, r.last_error, r.attempts, r.created_at,
        r.started_at, r.finished_at, r.progress, r.request_id, r.priority_class, r.source_key,
    )  # fmt: skip


_APPLY_JOB_KIND = "export.apply"  # `runtime_jobs.APPLY_JOB_KIND` (the composition root)


class OpsServices:
    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock,
        bus: EventBus,
        fsops: Fsops | Callable[[], Fsops | None] | None = None,
        targets: TargetOps | None = None,
        headless: HeadlessOps | None = None,
        restore: RestoreExit | None = None,
        credentials: CredentialsExit | None = None,
        keyring: KeyringExit | None = None,
        adoption: AdoptionExit | None = None,
        library: LibraryOps | None = None,
        intents: IntentOps | None = None,
        revisions: RevisionOps | None = None,
        backfill: BackfillOps | None = None,
        import_folder: ImportOps | None = None,
        normalize: NormalizeOps | None = None,
        source_migration: SourceMigrationOps | None = None,
        meta: MetaOps | None = None,
        content: ContentOps | None = None,
        rating: RatingOps | None = None,
        attach: AttachOps | None = None,
        edition_merge: EditionMergeOps | None = None,
        work_merge: WorkMergeOps | None = None,
        editions: EditionOps | None = None,
        dedup: DedupOps | None = None,
        resplit: ResplitOps | None = None,
        contributors: ContributorOps | None = None,
        export_items: ExportItemOps | None = None,
        export_all: ExportAllOps | None = None,
        wizard: WizardOps | None = None,
        follows: FollowOps | None = None,
        sheet: SheetOps | None = None,
        acquire: AcquireOps | None = None,
        requests: RequestService | None = None,
        roots: RootOps | None = None,
        maintenance: MaintenanceOps | None = None,
        kavita_accounts: KavitaAccountsOps | None = None,
        window: MaintenanceWindow | None = None,
    ) -> None:
        self._window = window or DEFAULT_WINDOW
        self._engine = engine
        self._clock = clock
        self._bus = bus
        self._fsops_source = fsops
        self._targets = targets
        self._headless = headless
        self._restore = restore
        self._credentials = credentials
        self._keyring = keyring
        self._adoption = adoption
        self._library = library
        self._intents = intents
        self._revisions = revisions
        self._backfill = backfill
        self._import_folder = import_folder
        self._normalize = normalize
        self._source_migration = source_migration
        self._meta = meta
        self._content = content
        self._rating = rating
        self._attach = attach
        self._edition_merge = edition_merge
        self._work_merge = work_merge
        self._editions = editions
        self._dedup = dedup
        self._resplit = resplit
        self._contributors = contributors
        self._export_items = export_items
        self._export_all = export_all
        self._wizard = wizard
        self._follows = follows
        self._sheet = sheet
        self._acquire = acquire
        self._requests = requests
        self._roots = roots
        self._maintenance = maintenance
        self._kavita_accounts = kavita_accounts

    @property
    def _fsops(self) -> Fsops | None:
        """The library as the roots are registered now: a root added by `target add` while the
        server runs is known to the next `series resume` or `target recheck` (a callable is asked
        at each use; a given `Fsops` is used as it is)."""
        source = self._fsops_source
        return source() if callable(source) else source

    # -- reading -------------------------------------------------------------------------------

    def status(self, *, full: bool = True) -> StatusReport:
        """What waits and which queues are paused; with `full`, also the sections of `shelfhold
        status` that are not waits (they cost a few reads per followed series)."""
        if self._roots is not None:
            self._roots.refresh()  # before the rest reads the marks: one view of the roots
        # The ports read the database on their own connections: asked while one is held here, a
        # burst of `/healthz` (one connection held, a second awaited) empties the pool and every
        # request waits for the others until the pool's timeout.
        waiting = [
            dress(item)
            for port in (
                self._restore,
                self._credentials,
                self._keyring,
                self._adoption,
                self._roots,
            )
            if port is not None
            for item in port.waiting()
        ]
        with self._engine.connect() as connection:
            items = collect(connection) + waiting
            if not full:
                return StatusReport(items, queues_of(connection, items))
            now = self._clock.now()
            return StatusReport(
                items, queues_of(connection, items), stale_report(connection, now),
                tombstoned_report(connection),
                [c.as_dict() for c in root_capabilities.read_all(connection)],
                known_bad_report(connection, now.date()), service_url_report(connection),
                unverifiable_download_dirs(connection), candidate_report(connection),
                jurisdiction_report(connection), rights_report(connection, now.date()),
                kept_in_service_report(connection), wal_pending_report(connection),
                koreader_reset_report(connection, now - KOREADER_RESET_WINDOW),
            )  # fmt: skip

    def restored_behind(self) -> bool:
        """The database is older than the disks: only the exits of the restore may act (I28)."""
        return self._restore is not None and bool(self._restore.waiting())

    def paused_queues(self) -> list[PausedQueue]:
        return self.status(full=False).queues

    def missing_list(self) -> list[WaitingItem]:
        return [i for i in self.status(full=False).waiting if i.state == "missing"]

    def jobs_list(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
        after: str | None = None,
    ) -> list[JobRow]:
        """Newest first (a ULID sorts by time); `after` is the id of the last row already served."""
        job = platform.job_view
        query = sa.select(job).order_by(job.c.id.desc()).limit(limit)
        if status is not None:
            query = query.where(job.c.status == status)
        if after is not None:
            query = query.where(job.c.id < after)
        with self._engine.connect() as connection:
            return [_job_row(r) for r in connection.execute(query)]

    def job_get(self, job_id: str) -> JobRow:
        """One job, for a client that was given its URL by a `202` (EF-INT-001)."""
        job = platform.job_view
        with self._engine.connect() as connection:
            row = connection.execute(sa.select(job).where(job.c.id == job_id)).first()
        if row is None:
            raise UnknownSubjectError("no such job")
        return _job_row(row)

    # -- missing / series / jobs / edition ------------------------------------------------------

    def missing_resolve(
        self, destination_id: str, export_item_id: str, action: str, *, actor_id: str | None
    ) -> None:
        """`reexport`: the next export puts the file back. `unfollow`: "stop following this
        chapter" (EF-EXP-018), with who and when, never lifted by itself."""
        if action not in {"reexport", "unfollow"}:
            raise InvalidChoiceError("choose `reexport` or `unfollow`")
        record, coverage = export.export_record, export.unit_coverage
        with self._engine.begin() as connection:
            row = connection.execute(
                sa.select(record.c.state).where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.export_item_id == export_item_id,
                )
            ).one_or_none()
            if row is None:
                raise UnknownSubjectError("no such record")
            if row.state != "missing":
                raise NotWaitingError("this file is not missing")
            where = (
                record.c.shelf_destination_id == destination_id,
                record.c.export_item_id == export_item_id,
            )
            if action == "reexport":
                connection.execute(
                    sa.update(record).where(*where).values(state="planned", error_code=None)
                )
                return
            units = connection.execute(
                sa.select(coverage.c.unit_id).where(
                    coverage.c.carrier_type == "export_item",
                    coverage.c.carrier_id == export_item_id,
                )
            ).scalars()
            for unit_id in units:
                suppress_follow(connection, unit_id, actor_id=actor_id, now=self._clock.now())
            connection.execute(sa.update(record).where(*where).values(state="recycled"))

    def series_resume(self, export_series_id: str) -> bool:
        """After the switch-over (or the rights back): what is on disk is the new baseline. A series
        paused for `permission_denied` is probed first (the same probe as `target recheck`)."""
        fsops = self._fsops
        if fsops is None:
            raise ExitUnavailableError("no library is mounted here")
        reconciler = Reconciler(self._engine, fsops, clock=self._clock, bus=self._bus)
        if not reconciler.resume_series(export_series_id):
            raise NotWaitingError("this series is not paused")
        return True

    def jobs_run_now(self, kind: str) -> None:
        """A periodic job is due now: the server's next tick (at most a minute) starts it."""
        table = platform.schedule
        now = self._clock.now()
        with self._engine.begin() as connection:
            rows = connection.execute(
                sa.select(table.c.id, table.c.window).where(
                    table.c.job_kind == kind, table.c.enabled
                )
            ).all()
            if rows and kind == FOLLOW_JOB_KIND:
                make_all_due(connection, now)  # by hand: every series is looked at (EF-SRC-038)
            for row in rows:
                values: dict[str, Any] = {"next_due_at": now}
                if row.window:
                    # a night job is planned for its next night: asked for now, it takes the
                    # window that is open now (else the next one: the disks stay asleep by day)
                    span = self._window.next_span(now)
                    values["window"] = {
                        "start_utc": span.start.strftime(WINDOW_FORMAT),
                        "end_utc": span.end.strftime(WINDOW_FORMAT),
                    }
                connection.execute(sa.update(table).where(table.c.id == row.id).values(**values))
        if not rows:
            raise UnknownSubjectError("no periodic job of this kind is scheduled")

    def jobs_retry_is_reserved(self, subject: str) -> bool:
        """The job applies a rename plan: putting it back in the queue is `plan.apply_rename`,
        which asks for a fresh admin session (EF-EXP-030, EF-USR-024), never a key."""
        job, plan = platform.job_view, coherence.export_plan
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(job.c.kind, job.c.dedupe_key).where(job.c.id == subject)
            ).first()
            if row is None or row.kind != _APPLY_JOB_KIND or not row.dedupe_key:
                return False
            kind = connection.execute(
                sa.select(plan.c.kind).where(plan.c.plan_id == row.dedupe_key.split(":")[1])
            ).scalar()
        return kind == "rename_plan"

    def jobs_retry(self, subject: str, *, principal: Principal | None = None) -> JobRow | None:
        """A job that gave up goes back to the queue (the scheduler dispatches it); a file the
        server never indexed is looked at again by the arrival poll. A job that applies a rename
        plan goes back only for a fresh admin session: a key (or a caller that says nobody) is
        refused, so that no replay brings an application the session did not give."""
        if self.jobs_retry_is_reserved(subject):
            verdict = (
                authorize_reserved(
                    principal, RESERVED_ACTIONS["plan.apply_rename"], now=self._clock.now()
                )
                if principal is not None
                else None
            )
            if not isinstance(verdict, Allowed):
                raise CodedRefusal("forbidden", "applying a rename plan needs a fresh session")
        job, record = platform.job_view, export.export_record
        with self._engine.begin() as connection:
            row = connection.execute(sa.select(job.c.status).where(job.c.id == subject)).first()
            if row is not None:
                if row.status not in DEAD:
                    raise NotWaitingError("this job did not give up")
                QueueControl().replay(connection, subject)  # the same rules as `jobs replay`
            else:
                cleared = connection.execute(
                    sa.update(record)
                    .where(record.c.export_item_id == subject, record.c.error_code == NOT_INDEXED)
                    .values(error_code=None)
                ).rowcount
                if not cleared:
                    raise UnknownSubjectError("no such job, and no file waiting for the index")
        found = [j for j in self.jobs_list() if j.id == subject]
        return found[0] if found else None

    def edition_set_lang(self, edition_id: str, lang: str) -> str:
        """Leave `language_unknown` (ENF-I18N-005): the tag is normalised, a placeholder or a
        collision with another Edition of the Work is refused, nothing is merged silently."""
        try:
            tag = str(edition_key_lang(lang))
        except LanguageError as error:
            raise LanguageRefusedError(f"language refused ({error.code})") from None
        table = catalog.edition
        with self._engine.begin() as connection:
            current = connection.execute(
                sa.select(table.c.lang).where(table.c.id == edition_id)
            ).first()
            if current is None:
                raise UnknownSubjectError("no such Edition")
            if current.lang != UNKNOWN_LANGUAGE:
                raise NotWaitingError("the language of this Edition is known")
            try:
                with connection.begin_nested():
                    connection.execute(
                        sa.update(table)
                        .where(table.c.id == edition_id)
                        .values(lang=tag, lang_raw=lang, lang_norm_by=LANG_NORMALIZER_ID)
                    )
            except sa.exc.IntegrityError:
                raise LanguageRefusedError(
                    "language already used by another Edition of this Work"
                ) from None
        return tag

    def edition_set_series_name(self, edition_id: str, name: str) -> tuple[str, str]:
        """Leave `series_name_collision` (EF-EXP-032): the exported series of this Edition gets
        the name the administrator chose, the first export then writes into it. Refused when the
        name is taken in the library too, or when the Edition does not wait for a name."""
        refusal = series_name_refusal(name)
        if refusal is not None:
            raise InvalidChoiceError(refusal)
        clean = " ".join(name.split())
        normalized = normalize_series_name(clean)
        if not clean or not normalized or len(clean) > MAX_SERIES_NAME:
            raise InvalidChoiceError("give a series name of letters and digits, 200 characters")
        with self._engine.begin() as connection:
            mark = waiting_marks.read_one(connection, "series_name_collision", edition_id)
            if mark is None:
                raise NotWaitingError("this Edition does not wait for a series name")
            ctx = load_context(connection, mark["destination_id"])
            if ctx.remote_library_id is None:
                raise NotWaitingError("the destination has no remote library any more")
            if normalized in load_foreign_names(connection, ctx.destination_id):
                raise CodedRefusal(  # a series of another source already bears it in Kavita
                    "export.series_name_collision", "Kavita already shows a series with this name"
                )
            try:
                series_id = ensure_export_series(
                    connection, shelf_destination_id=ctx.destination_id,
                    remote_library_id=ctx.remote_library_id, now=self._clock.now(),
                    ref=SeriesRef(edition_id, clean, normalized, proposal_key={"chosen": "admin"}),
                    assigned_by="plan",
                )  # fmt: skip
            except SeriesCollisionError:
                raise CodedRefusal(
                    "export.series_name_collision", "another series already has this name"
                ) from None
            waiting_marks.clear(connection, "series_name_collision", [edition_id])
        return series_id, clean

    # -- targets -------------------------------------------------------------------------------

    def _destinations(self, target_id: str) -> list[str]:
        table = export.shelf_destination
        with self._engine.connect() as connection:
            return list(
                connection.execute(
                    sa.select(table.c.id).where(table.c.target_id == target_id).order_by(table.c.id)
                ).scalars()
            )

    def _require_target(self, target_id: str) -> None:
        with self._engine.connect() as connection:
            known = connection.execute(
                sa.select(export.export_target.c.id).where(export.export_target.c.id == target_id)
            ).first()
        if known is None:
            raise UnknownSubjectError("no such target")

    def target_recheck_is_reserved(self, target_id: str) -> bool:
        """Letting a shelf out of `child_exposure` is parental control: the route asks for a fresh
        session (EF-USR-024). Every other recheck is an ordinary admin action."""
        self._require_target(target_id)
        with self._engine.connect() as connection:
            health = load_health(connection, target_id)
            exposed = connection.execute(
                sa.select(sa.func.count())
                .select_from(export.shelf)
                .where(export.shelf.c.paused_reason == "child_exposure")
            ).scalar_one()
        return health.child_exposure or bool(exposed)

    def target_recheck(self, target_id: str) -> RecheckOutcome:
        """`target recheck`: the rights of the folders, the routes of the server, the exposure of
        the libraries are looked at again; what passes is freed, the rest keeps its cause."""
        self._require_target(target_id)
        cleared: list[str] = []
        denied: list[str] = []
        fsops = self._fsops
        if fsops is not None:
            preflight = PermissionPreflight(self._engine, fsops, self._bus, clock=self._clock)
            for destination in self._destinations(target_id):
                report = preflight.recheck(destination)
                cleared += report.cleared
                denied += report.denied
        blocked = self._recheck_mounts(fsops, target_id)
        with self._engine.connect() as connection:
            waiting = load_health(connection, target_id).waiting_state
        if self._targets is not None:
            self._targets.recheck(target_id)
        elif waiting is not None:
            raise ExitUnavailableError("this instance has no Kavita connection")
        if blocked:
            raise StillWaitingError("the mount of the library still lacks its sentinel")
        return RecheckOutcome(tuple(cleared), tuple(denied))

    def _recheck_mounts(self, fsops: Fsops | None, target_id: str) -> list[str]:
        """`mount_sentinel_missing` (EF-EXP-029) leaves once the sentinel of the root is the one
        expected again: it is read here, on request, and never by a periodic look. The
        destinations whose mount is still wrong are returned."""
        if fsops is None:
            return []
        blocked: list[str] = []
        for destination in self._destinations(target_id):
            with self._engine.connect() as connection:
                mark = waiting_marks.read_one(connection, "mount_sentinel_missing", destination)
                if mark is None:
                    continue
                root = load_context(connection, destination).root_local
            if root is not None and fsops.status(root).state is not RootState.OK:
                blocked.append(destination)
            else:
                waiting_marks.clear_mount_blocked(self._engine, destination)
        return blocked

    def target_reconnect(self, target_id: str) -> None:
        self._require_target(target_id)
        if self._targets is None:
            raise ExitUnavailableError("this instance has no Kavita connection")
        self._targets.reconnect(target_id)

    def target_rotate_key(self, target: str, *, password: str) -> None:
        if self._headless is None:
            raise ExitUnavailableError("the configuration facade is not connected")
        self._headless.target_rotate_key(target=target, password=password)

    # -- sources -------------------------------------------------------------------------------

    def source_set_secret(self, secret: str) -> None:
        if self._headless is None:
            raise ExitUnavailableError("the configuration facade is not connected")
        self._headless.source_set_secret(secret)

    def source_recheck(self) -> None:
        if self._headless is None:
            raise ExitUnavailableError("the configuration facade is not connected")
        self._headless.source_recheck()

    def source_reconnect(self) -> None:
        if self._headless is None:
            raise ExitUnavailableError("the configuration facade is not connected")
        self._headless.source_reconnect()

    # -- the ports of the other lots ------------------------------------------------------------

    def restore_adopt(self, *, confirm_deletion: bool, principal: Principal) -> None:
        if self._restore is None:
            raise ExitUnavailableError("the restore guard is not connected")
        self._restore.adopt_after_restore(confirm_deletion=confirm_deletion, principal=principal)

    def restore_forget_root(self, *, root_id: str, confirm: str, principal: Principal) -> None:
        if self._restore is None:
            raise ExitUnavailableError("the restore guard is not connected")
        self._restore.forget_root(root_id=root_id, confirm=confirm, principal=principal)

    def restore_acknowledge(self, *, principal: Principal) -> list[str]:
        """The passkeys created or revoked since the backup, as far as they are known."""
        if self._credentials is None:
            raise ExitUnavailableError("the restore notice is not connected")
        return self._credentials.acknowledge(principal=principal)

    def tombstone_accept_lost_keyring(
        self, *, confirmation: str, principal: Principal
    ) -> list[str]:
        if self._keyring is None:
            raise ExitUnavailableError("the tombstone service is not connected")
        # An exit of the freeze, on every door: without an export it is the only way out of
        # `key_unknown`, which blocks the adoption. Under the freeze the phrase is longer (it
        # says what the waiver costs), the journals are read and none is written.
        return self._keyring.accept_lost_keyring(
            confirmation=confirmation, principal=principal, frozen=self.restored_behind()
        )

    def secrets_import(
        self, *, passphrase_file: str, in_file: str, principal: Principal,
        confined: bool = False,
    ) -> dict[str, Any]:  # fmt: skip
        """`secrets import` (reserved): the keys of an age export merged into the keyring, then
        the tombstone lines replayed.

        It is an exit of the freeze for its `/data` half only (keyring, registry, audit): a
        restore from before a rotation is blocked by `key_unknown` until the keys are back, and
        `reconcile --adopt-after-restore` is the one that replays, with the keyring on disk. So
        under `restored_behind` nothing is replayed here (no root is read or written)."""
        if self._keyring is None:
            raise ExitUnavailableError("the tombstone service is not connected")
        return self._keyring.import_keyring(
            passphrase_file=passphrase_file, in_file=in_file, principal=principal,
            replay=not self.restored_behind(), confined=confined,
        )  # fmt: skip

    def tombstone_set_aside_journal(
        self, *, principal: Principal, root_id: str | None = None
    ) -> dict[str, str]:
        if self._keyring is None:
            raise ExitUnavailableError("the tombstone service is not connected")
        return self._keyring.set_aside_journal(principal=principal, root_id=root_id)

    def tombstone_rekey(self, *, principal: Principal, kids: Sequence[str]) -> dict[str, Any]:
        """After a key leak (reserved, host CLI): the keys named are marked compromised and the
        tombstones sealed again under the active key (EF-BIB-054 (10))."""
        if self._keyring is None:
            raise ExitUnavailableError("the tombstone service is not connected")
        return self._keyring.rekey(principal=principal, kids=kids)

    def tombstone_flush(self) -> list[str]:
        """Write the lines the mounted roots owe; the roots that still owe some."""
        if self._keyring is None:
            raise ExitUnavailableError("the tombstone service is not connected")
        return self._keyring.flush_owed()

    @property
    def roots(self) -> RootOps:
        """`root takeover|reinit|restored`, `store reinit`, `--rebaseline-inodes` (EF-EXP-064)."""
        if self._roots is None:
            raise ExitUnavailableError("the roots are not connected")
        return self._roots

    def root_takeover(self, subject: str, *, principal: Principal) -> dict[str, Any]:
        return self.roots.takeover(subject, principal=principal)

    def root_reinit(self, subject: str, *, principal: Principal) -> dict[str, Any]:
        return self.roots.reinit(subject, principal=principal)

    def root_restored(self, subject: str, *, principal: Principal) -> dict[str, Any]:
        return self.roots.restored(subject, principal=principal)

    def store_reinit(self, *, principal: Principal) -> dict[str, Any]:
        return self.roots.store_reinit(principal=principal)

    def reconcile_rebaseline_inodes(self, destination_id: str | None = None) -> dict[str, Any]:
        return self.roots.rebaseline_inodes(destination_id)

    @property
    def library(self) -> LibraryOps:
        """Pairing, repatriation, detachment, purge and the write probe (L30, L31, L34, L37)."""
        if self._library is None:
            raise ExitUnavailableError("the library operations are not connected")
        return self._library

    @property
    def attach(self) -> AttachOps:
        """The attaches by external identifier: list, "Séparer", confirm (EF-BIB-003, L12)."""
        if self._attach is None:
            raise ExitUnavailableError("the attaches are not connected")
        return self._attach

    @property
    def edition_merge(self) -> EditionMergeOps:
        """`edition merge`: the plan of a merge of two Editions and its application (EF-BIB-049)."""
        if self._edition_merge is None:
            raise ExitUnavailableError("the merge of Editions is not connected")
        return self._edition_merge

    @property
    def work_merge(self) -> WorkMergeOps:
        """Merging Works, « Séparer » and the exported-series plan (M3 L22, EF-BIB-019)."""
        if self._work_merge is None:
            raise ExitUnavailableError("the merge of Works is not connected")
        return self._work_merge

    @property
    def dedup(self) -> DedupOps:
        """The suggestions of duplicate Works: scan and list, never a merge (EF-BIB-004)."""
        if self._dedup is None:
            raise ExitUnavailableError("the duplicate suggestions are not connected")
        return self._dedup

    @property
    def editions(self) -> EditionOps:
        """Field locks, renumbered identifiers and the sorted list (EF-BIB-008, EF-BIB-003)."""
        if self._editions is None:
            raise ExitUnavailableError("the Edition commands are not connected")
        return self._editions

    @property
    def resplit(self) -> ResplitOps:
        """`coverage resplit propose|apply`: a chapter the source cut again (EF-BIB-044)."""
        if self._resplit is None:
            raise ExitUnavailableError("the re-cut chapters are not connected")
        return self._resplit

    @property
    def kavita_accounts(self) -> KavitaAccountsOps:
        """The audit of the Kavita accounts of the children, read only (EF-EXP-036)."""
        if self._kavita_accounts is None:
            raise ExitUnavailableError("the Kavita accounts audit is not connected")
        return self._kavita_accounts

    @property
    def maintenance(self) -> MaintenanceOps:
        """`maintenance on|off` and the control of the queue (EF-INT-018, EF-INT-017)."""
        if self._maintenance is None:
            raise ExitUnavailableError("the maintenance mode is not connected")
        return self._maintenance

    def maintenance_active(self) -> bool:
        """While it is on, only reads, `/healthz` and the exit go through (the socket and the
        routes both ask this before they let a write in)."""
        return self._maintenance is not None and self._maintenance.mode.active()

    @property
    def contributors(self) -> ContributorOps:
        """`contributor set-died-for-france` (EF-SRC-024)."""
        if self._contributors is None:
            raise ExitUnavailableError("the contributor commands are not connected")
        return self._contributors

    @property
    def export_all(self) -> ExportAllOps:
        """`export-all` and `import-all`: the whole library in the open format (EF-INT-016)."""
        if self._export_all is None:
            raise ExitUnavailableError("the open export is not connected")
        return self._export_all

    @property
    def wizard(self) -> WizardOps:
        """The first-run wizard: the folder probe read by Kavita and the adoption (L39)."""
        if self._wizard is None:
            raise ExitUnavailableError("the first-run wizard is not connected")
        return self._wizard

    @property
    def export_items(self) -> ExportItemOps:
        """`export item show`: a volume, its chapters and its kept file (EF-BIB-044)."""
        if self._export_items is None:
            raise ExitUnavailableError("the export items are not connected")
        return self._export_items

    @property
    def rating(self) -> RatingOps:
        """`rate`, and the batch of "À classer" (EF-INT-014, EF-EXP-048)."""
        if self._rating is None:
            raise ExitUnavailableError("rating is not connected")
        return self._rating

    @property
    def sheet(self) -> SheetOps:
        """The series sheet: one Edition's bindings, gaps, teams, fields and settings (L43)."""
        if self._sheet is None:
            raise ExitUnavailableError("the series sheet is not connected")
        return self._sheet

    @property
    def follows(self) -> FollowOps:
        """`follow` and `unfollow` (EF-SRC-029, EF-INT-014)."""
        if self._follows is None:
            raise ExitUnavailableError("following is not connected")
        return self._follows

    @property
    def requests(self) -> RequestService:
        """Ask, approve, refuse, arrive (EF-USR-010)."""
        if self._requests is None:
            raise ExitUnavailableError("requests are not connected")
        return self._requests

    @property
    def acquire(self) -> AcquireOps:
        """`source search|filters`, `acquire` (EF-SRC-015, EF-SRC-004)."""
        if self._acquire is None:
            raise ExitUnavailableError("acquisition is not connected")
        return self._acquire

    @property
    def intents(self) -> IntentOps:
        """`intents list|resolve` (EF-INT-014)."""
        if self._intents is None:
            raise ExitUnavailableError("the intent journal is not connected")
        return self._intents

    @property
    def revisions(self) -> RevisionOps:
        """`revisions list|apply` (EF-BIB-043)."""
        if self._revisions is None:
            raise ExitUnavailableError("the revision queue is not connected")
        return self._revisions

    @property
    def backfill(self) -> BackfillOps:
        """`backfill --gaps|--all` (EF-EXP-053)."""
        if self._backfill is None:
            raise ExitUnavailableError("backfill is not connected")
        return self._backfill

    @property
    def import_folder(self) -> ImportOps:
        """`import scan` (EF-SRC-012)."""
        if self._import_folder is None:
            raise ExitUnavailableError("the import folder is not connected")
        return self._import_folder

    @property
    def normalize(self) -> NormalizeOps:
        """`normalize migrate` (ENF-OPS-021, EF-BIB-054)."""
        if self._normalize is None:
            raise ExitUnavailableError("normalizer migration is not connected")
        return self._normalize

    @property
    def source_migration(self) -> SourceMigrationOps:
        """`source migrate-ids` (EF-SRC-022, EF-BIB-054)."""
        if self._source_migration is None:
            raise ExitUnavailableError("the identity migration of a source is not connected")
        return self._source_migration

    @property
    def meta(self) -> MetaOps:
        """`meta rebuild|check` (ENF-OPS-020, EF-EXP-063)."""
        if self._meta is None:
            raise ExitUnavailableError("the projections are not connected")
        return self._meta

    @property
    def content(self) -> ContentOps:
        """`content reneutralize` (EF-BIB-051)."""
        if self._content is None:
            raise ExitUnavailableError("the content pipeline is not connected")
        return self._content

    def adopt_set_follow_from(self, *, destination_id: str, series: str, chapter: str) -> None:
        if self._adoption is not None:
            self._adoption.set_follow_from(
                destination_id=destination_id, series=series, chapter=chapter
            )
            return
        self.library.set_follow_from(destination_id=destination_id, series=series, chapter=chapter)

    def adopt_set_coverage(
        self, *, destination_id: str, series: str, volume: str, first: str, last: str
    ) -> int:
        """`adopt set-coverage`: the chapters an adopted volume holds (EF-BIB-044)."""
        return self.library.set_coverage(
            destination_id=destination_id, series=series, volume=volume, first=first, last=last
        )

    def adopt_confirm_migration(self, *, destination_id: str, fingerprint: str) -> None:
        if self._adoption is not None:
            self._adoption.confirm_migration(destination_id=destination_id, fingerprint=fingerprint)
            return
        self.library.confirm_migration(destination_id=destination_id, fingerprint=fingerprint)
