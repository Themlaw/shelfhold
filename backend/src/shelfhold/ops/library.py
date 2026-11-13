"""The operations on the library that had a service but no command (L30, L31, L34, L37).

Pairing and `set-follow-from` (L30), the confirmation of the switch-over (L30), the repatriation
and the detachment (L31), `purge --now` (L34), the write probe (L37). Each method opens what it
needs from the composition at the time of the call (the lease only for what writes), takes and
returns plain values: the CLI, the socket and the HTTP routes all call this one class, and the
reserved classification is theirs (the route or the socket decides before calling).
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa
import structlog

from shelfhold.acquisition.application.upstream import (
    Detacher,
    DetachScope,
    UpstreamSnapshots,
)
from shelfhold.acquisition.public import decline_pairing
from shelfhold.catalog.public import (
    ListedUnit,
    RelinkRefusedError,
    RelinkRequest,
    RelinkResult,
    relink_isolated,
    translation_type_for,
)
from shelfhold.composition_errors import KeyringUnavailableError, NoLibraryError
from shelfhold.export.application.coverage import (
    CoverageError,
    flag_unknown_coverage,
    set_volume_coverage,
)
from shelfhold.export.application.deletion import (
    DeletionBlockedError,
    DeletionService,
    DeletionUnknownError,
    RestoreBlockedError,
)
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import LockBusyError
from shelfhold.export.application.migration_detect import (
    STEPS,
    SwitchoverDetector,
    SwitchoverGate,
    SwitchoverIncompleteError,
)
from shelfhold.export.application.pairing import (
    AutoPairReport,
    Pairing,
    PairingResult,
    Proposal,
)
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.preflight import PermissionPreflight
from shelfhold.export.application.purge import (
    PurgeCollateralError,
    PurgeRefusedError,
    PurgeReport,
    PurgeService,
    PurgeUnknownError,
)
from shelfhold.export.application.purge_leftovers import PurgeLeftovers
from shelfhold.export.application.reclaim import TrashEmptyRefusedError
from shelfhold.export.application.repatriate import (
    RepatriationBlockedError,
    RepatriationCheck,
    RepatriationPlan,
    Repatriator,
)
from shelfhold.export.application.repatriate_kavita import (
    BaselineStore,
    KavitaVerdict,
    covers,
    read_snapshot,
    verify,
)
from shelfhold.export.application.restore import RestoredBehindError
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.application.withdrawal import (
    NotChildLibraryError,
    WithdrawalBusyError,
    WithdrawalPlan,
    WithdrawalRefusedError,
    WithdrawalRootReadOnlyError,
    WithdrawalService,
)
from shelfhold.export.domain.path_map import PathMapError
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.series import refresh_names
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    AuditEntry,
    Principal,
    audit_insert,
    authorize_reserved,
)
from shelfhold.infrastructure.fsops.errors import RootBlockedError
from shelfhold.infrastructure.fsops.probe import probe_folder
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity, platform
from shelfhold.infrastructure.wal import WalBusyError, truncate_wal
from shelfhold.jobs.maintenance import MaintenanceBusyError, MaintenanceGate, compact_database
from shelfhold.library.public import (
    KEPT_PREFIX,
    ConfirmationRequiredError,
    DeletionLevel,
    DeletionPlan,
    DeletionRefusedError,
    StoreIndex,
    TrashConfirmationError,
)
from shelfhold.library.tombstones.lift import (
    LiftConfirmationError,
    LiftRefusedError,
    UnknownTombstoneError,
)
from shelfhold.ops import reclaim
from shelfhold.ops.backup.report import retained_since
from shelfhold.ops.backup.scratch import sweep_orphans
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError, OpsError, UnknownSubjectError
from shelfhold.purge_upstream import connected_upstream
from shelfhold.sdk.refs import SeriesRef
from shelfhold.work_locks import index_holder

if TYPE_CHECKING:
    from shelfhold.composition import Composition, WriteContext
    from shelfhold.infrastructure.fsops import Fsops

__all__ = ["LibraryOps"]

_log = structlog.get_logger("shelfhold.export")

# Kavita cannot be asked: the control refuses instead of guessing
_KAVITA_UNAVAILABLE: tuple[type[Exception], ...] = (
    TargetError, KeyringUnavailableError, sa.exc.NoResultFound, KeyError,
)  # fmt: skip
SERVICE_ID = "suwayomi"  # the one connected service of M1a
AUTO_MIN_SCORE = 0.9


def _proposal(p: Proposal) -> dict[str, Any]:
    return {
        "export_series_id": p.export_series_id,
        "series": p.series_name,
        "state": p.state,
        "candidates": [
            {"source_key": c.ref.source_key, "remote_id": c.ref.remote_id, "score": c.score}
            for c in p.candidates
        ],
        "follow_from": p.follow_from,
        "gaps": p.gaps,
        "command": p.exit_command,
    }


def _result(r: PairingResult) -> dict[str, Any]:
    return {
        "state": r.state,
        "export_series_id": r.export_series_id,
        "binding_id": r.binding_id,
        "follow_from": r.follow_from,
        "command": r.exit_command,
    }


def _plan(plan: RepatriationPlan) -> dict[str, Any]:
    return {
        "fingerprint": plan.fingerprint,
        "source_root": plan.source_root,
        "target_root": plan.target_root,
        "series": plan.series,
        "chapters": plan.chapters,
        "files": len(plan.files),
        "bytes_to_copy": plan.bytes_to_copy,
        "free_bytes": plan.free_bytes,
        "ready": plan.ready,
        "frozen": plan.frozen,
        "problems": [{"path": a.rel_path, "status": a.status} for a in plan.problems],
    }


def _kavita(verdict: KavitaVerdict | None) -> dict[str, Any]:
    if verdict is None:
        return {"verified": False, "reason": "not_asked", "problems": []}
    return {
        "verified": verdict.verified,
        "reason": verdict.reason,
        "series": [verdict.series_before, verdict.series_after],
        "files": [verdict.files_before, verdict.files_after],
        "problems": [{"kind": kind, "what": what} for kind, what in verdict.problems],
    }


def _relinked(r: RelinkResult) -> dict[str, Any]:
    return {
        "binding_id": r.binding_id,
        "edition_id": r.edition_id,
        "edition_created": r.edition_created,
        "missing": [m.raw_number if m.raw_number is not None else "" for m in r.missing],
        "to_rate": r.to_rate,
        "carried_teams": list(r.carried_teams),
    }


def _withdrawal_plan(plan: WithdrawalPlan) -> dict[str, Any]:
    return {
        "version": plan.version,
        "items": [
            {
                "edition_id": i.edition_id,
                "shelf_id": i.shelf_id,
                "reason": i.reason,
                "files": [f.rel_path for f in i.files],
            }
            for i in plan.items
        ],
    }


def _deletion_plan(plan: DeletionPlan) -> dict[str, Any]:
    """The preview: what each file is (its place, its path in its root, its size) and codes;
    never a title."""
    return {
        "level": plan.level.value,
        "version": plan.version,
        "blocked": plan.blocked,
        "files": plan.files,
        "bytes": plan.bytes,
        "folders": list(plan.folders),
        "confirmations": list(plan.confirmations),
        "units": [
            {
                "unit_id": u.unit_id,
                "irreplaceable": u.irreplaceable,
                "independent_copies": u.independent_copies,
                "independent_copies_after": u.independent_copies_after,
                "files": [
                    {
                        "place": f.place,
                        "path": f.rel_path,
                        "size": f.size,
                        "export_item_id": f.export_item_id,
                    }
                    for f in u.files
                ],
                "untouched": list(u.untouched),
                "blocked": list(u.blocked),
                "exports": u.exports,
                "progress": u.progress,
                "needs_confirmation": u.needs_confirmation,
            }
            for u in plan.units
        ],
    }


def _check(check: RepatriationCheck) -> dict[str, Any]:
    return {
        "series": check.series,
        "chapters": check.chapters,
        "files": check.files,
        "missing": list(check.missing),
        "different": list(check.different),
        "files_intact": check.files_intact,
        "kavita": _kavita(check.kavita),
        "safe_to_unmount": check.safe_to_unmount,
    }


OWN_CODES = frozenset({"pairing", "repatriate", "backfill", "instance"})
# exceptions of the services that carry no catalog code of their own: (class, code)
_CODED: tuple[tuple[type[Exception], str], ...] = (
    (PurgeUnknownError, "ops.unknown_subject"),
    (PurgeCollateralError, "purge.collateral"),
    (WalBusyError, "purge.wal_busy"),
    (LockBusyError, "purge.busy"),
    (MaintenanceBusyError, "purge.busy"),
    (ConfirmationRequiredError, "unit.confirmation_required"),
    (PurgeRefusedError, "forbidden"),
    (DeletionRefusedError, "forbidden"),
    (DeletionBlockedError, "deletion.blocked"),
    (RestoreBlockedError, "deletion.blocked"),
    (DeletionUnknownError, "ops.unknown_subject"),
    (SwitchoverIncompleteError, "pairing.switchover_unconfirmed"),
    (NoLibraryError, "ops.exit_unavailable"),
    (KeyringUnavailableError, "ops.exit_unavailable"),
    (RelinkRefusedError, "forbidden"),
    (TrashEmptyRefusedError, "forbidden"),  # M3 L26: a key, a member, a stale session
    (TrashConfirmationError, "trash.confirmation_required"),
    (LiftRefusedError, "forbidden"),
    (LiftConfirmationError, "tombstone.confirmation_required"),
    (UnknownTombstoneError, "tombstone.unknown"),
)


def refusal_of(error: Exception) -> OpsError | None:
    """The refusal that stands for a service's exception, or None when it is a real failure."""
    if isinstance(error, sa.exc.OperationalError | sqlite3.OperationalError) and (
        "locked" in str(error).lower()
    ):  # the database is held by another writer: ask again in a moment
        return CodedRefusal("purge.busy", "database is locked")
    if isinstance(error, RootBlockedError):  # the exit command is in the message and the catalog
        frozen = error.state is RootState.RESTORED_BEHIND
        code = "instance.restored_behind" if frozen else "root.blocked"
        return CodedRefusal(code, str(error))
    if isinstance(error, RestoredBehindError):
        return CodedRefusal("instance.restored_behind", str(error))
    for kind, code in _CODED:
        if isinstance(error, kind):
            return CodedRefusal(code, type(error).__name__)
    named = getattr(error, "code", None)
    if isinstance(named, str) and named.split(".", 1)[0] in OWN_CODES:
        return CodedRefusal(named, type(error).__name__)
    return None


def _refused[F: Callable[..., Any]](method: F) -> F:
    @functools.wraps(method)
    def call(*args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except OpsError:
            raise
        except Exception as error:
            refusal = refusal_of(error)
            if refusal is None:
                raise
            raise refusal from None

    return cast("F", call)


class LibraryOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp
        self._nature = "unknown"  # what the connected service declares, once it has been read

    def _destination(self, destination_id: str | None) -> str:
        """The destination named, or the only tree destination there is."""
        table = export.shelf_destination
        if destination_id:
            with self._comp.engine.connect() as connection:
                known = connection.execute(
                    sa.select(table.c.id).where(table.c.id == destination_id)
                ).first()
            if known is None:  # an id nobody holds is a 404, not a failure further down
                raise UnknownSubjectError("unknown destination")
            return destination_id
        with self._comp.engine.connect() as connection:
            found = list(
                connection.execute(
                    sa.select(table.c.id).where(table.c.transport == "tree")
                ).scalars()
            )
        if len(found) != 1:
            raise UnknownSubjectError("give --destination: there is not exactly one")
        return str(found[0])

    # -- the connected service -----------------------------------------------------------------

    def _suwayomi(self) -> Any:
        link = self._comp.suwayomi()
        if link is None:
            raise ExitUnavailableError("no Suwayomi is configured: `shelfhold source add suwayomi`")
        return link

    def _library(self) -> tuple[Any, list[SeriesRef]]:
        """The series Suwayomi follows now (the library the pairing proposes from)."""
        link = self._suwayomi()
        self._nature = str(getattr(link.service.manifest, "translation_nature", "unknown"))

        async def read() -> list[SeriesRef]:
            try:
                return list(await link.service.followed_series(link.fetcher))
            finally:
                await link.guarded.aclose()

        return link, asyncio.run(read())

    def _listing(
        self, source_key: str, remote_id: str
    ) -> tuple[SeriesRef | None, list[ListedUnit]]:
        """The series Suwayomi follows under this source, with the releases it lists now."""
        link = self._suwayomi()
        self._nature = str(getattr(link.service.manifest, "translation_nature", "unknown"))

        async def read() -> tuple[SeriesRef | None, list[ListedUnit]]:
            try:
                library = await link.service.followed_series(link.fetcher)
                ref = next(
                    (r for r in library if (r.source_key, r.remote_id) == (source_key, remote_id)),
                    None,
                )
                if ref is None:
                    return None, []
                units = await link.service.list_units(link.fetcher, ref)
                return ref, [ListedUnit(u.number, u.source_order) for u in units]
            finally:
                await link.guarded.aclose()

        return asyncio.run(read())

    def _require_not_restored(self) -> None:
        """No write into the catalog of an instance restored behind its disks (EF-INT-026)."""
        loaded = self._comp.load_roots()
        if loaded is not None and loaded.restore_pending:
            raise RestoredBehindError(
                "restored_behind: run `shelfhold reconcile --adopt-after-restore`"
            )

    def _refresh_names(self, destination_id: str) -> None:
        """Kavita may have renamed a series: the names the pairing compares are the current ones
        (EF-EXP-057). A Kavita that cannot be asked leaves the cache as it is."""
        table = export.shelf_destination
        try:
            self._require_not_restored()
            with self._comp.engine.connect() as connection:
                target_id = connection.execute(
                    sa.select(table.c.target_id).where(table.c.id == destination_id)
                ).scalar_one()
            client = self._comp.kavita_client(str(target_id))
            asyncio.run(refresh_names(self._comp.engine, client, destination_id))
        except (
            KeyringUnavailableError,
            TargetError,
            KeyError,
            sa.exc.NoResultFound,
            RestoredBehindError,
        ):
            return

    def _gate(self, fsops: Fsops, destination_id: str) -> SwitchoverGate:
        """The switch-over gate, with the folder the Suwayomi server downloads to (the signal
        `download_dir` of EF-EXP-044) read from that server now. A server that cannot be asked
        leaves the folder declared before, if any."""
        gate = SwitchoverGate(
            self._comp.engine, SwitchoverDetector(self._comp.engine, fsops), clock=self._comp.clock
        )
        link = self._comp.suwayomi()
        if link is None:
            return gate

        async def read() -> str | None:
            try:
                return await link.service.download_dir(link.fetcher)  # type: ignore[no-any-return]
            finally:
                await link.guarded.aclose()

        try:
            path = asyncio.run(read())
        except Exception:  # any failure of the service: the signal stays as it was
            _log.warning("switchover.download_dir_unreadable", destination=destination_id)
            return gate
        if path is not None:
            gate.declare_download_dir(destination_id, path)
        return gate

    def _pairing(self, w: WriteContext, destination_id: str) -> Pairing:
        return Pairing(
            self._comp.engine,
            PermissionPreflight(self._comp.engine, w.fsops, w.bus, clock=self._comp.clock),
            self._gate(w.fsops, destination_id),
            clock=self._comp.clock,
            tombstones=w.tombstones,
            translation_nature=self._nature,
        )

    # -- relink (EF-BIB-029) -------------------------------------------------------------------

    @_refused
    def relink(
        self,
        work_id: str,
        *,
        source_key: str,
        remote_id: str,
        lang: str | None,
        replaces: str | None,
        principal: Principal,
    ) -> dict[str, Any]:
        """Attach the series Suwayomi follows under another source to a Work that exists. Nothing
        is fetched here: the next follow cycle asks for the numbers the Edition lacks."""
        ref, listed = self._listing(source_key, remote_id)
        if ref is None:
            raise UnknownSubjectError("Suwayomi does not follow that series")
        result = relink_isolated(
            self._comp.engine,
            RelinkRequest(
                work_id=work_id, plugin_id=SERVICE_ID, source_key=ref.source_key,
                remote_id=ref.remote_id, listed=tuple(listed), lang=lang,
                replaces_binding_id=replaces, translation_type=translation_type_for(self._nature),
                memo=ref.memo, memo_version=ref.memo_version,
            ),
            principal=principal, now=self._comp.clock.now(),
        )  # fmt: skip
        return _relinked(result)

    # -- pairing (L30) -------------------------------------------------------------------------

    @_refused
    def pair_propose(self, destination_id: str | None) -> list[dict[str, Any]]:
        destination_id = self._destination(destination_id)
        self._refresh_names(destination_id)
        _link, library = self._library()
        with self._comp.write_context() as w:
            return [
                _proposal(p)
                for p in self._pairing(w, destination_id).propose(destination_id, library)
            ]

    @_refused
    def pair_confirm(
        self,
        destination_id: str | None,
        export_series_id: str,
        *,
        source_key: str,
        remote_id: str,
        follow_from: str | None = None,
        confirm_backfill: int | None = None,
    ) -> dict[str, Any]:
        destination_id = self._destination(destination_id)
        self._refresh_names(destination_id)
        link, library = self._library()
        ref = next(
            (r for r in library if (r.source_key, r.remote_id) == (source_key, remote_id)), None
        )
        if ref is None:
            raise UnknownSubjectError("Suwayomi does not follow that series")
        with self._comp.write_context() as w:
            result = self._pairing(w, destination_id).confirm(
                destination_id, export_series_id, ref, follow_from=follow_from,
                confirm_backfill=confirm_backfill,
            )  # fmt: skip
        self._snapshot(link)
        return _result(result)

    @_refused
    def pair_decline(self, export_series_id: str) -> dict[str, Any]:
        """An adopted series has no twin in the Suwayomi library: what looks like one is created
        as a series of its own at the next follow cycle (L42)."""
        series = export.export_series
        with self._comp.engine.connect() as connection:
            known = connection.execute(
                sa.select(series.c.id).where(series.c.id == export_series_id)
            ).first()
        if known is None:
            raise UnknownSubjectError("no such adopted series")
        decline_pairing(self._comp.engine, self._comp.clock, export_series_id)
        return {"export_series_id": export_series_id, "state": "declined"}

    @_refused
    def pair_auto(
        self,
        destination_id: str | None,
        *,
        min_score: float = AUTO_MIN_SCORE,
        fingerprint: str | None,
    ) -> dict[str, Any]:
        """Without `fingerprint`: the report of what a batch would pair, nothing bound. With the
        fingerprint of that report: the batch, once, all or nothing."""
        destination_id = self._destination(destination_id)
        link, library = self._library()
        with self._comp.write_context() as w:
            pairing = self._pairing(w, destination_id)
            if fingerprint is None:
                report: AutoPairReport = pairing.pair_auto(
                    destination_id, library, min_score=min_score
                )
                return {
                    "fingerprint": report.fingerprint,
                    "ready": [_proposal(p) for p in report.ready],
                    "held": [_proposal(p) for p in report.held],
                }
            batch = pairing.confirm_batch(
                destination_id, library, fingerprint=fingerprint, min_score=min_score
            )
        if batch.complete:
            self._snapshot(link)
        return {
            "paired": [_result(r) for r in batch.paired],
            "refused": [{"export_series_id": s, "reason": str(e)} for s, e in batch.refused],
        }

    @_refused
    def set_follow_from(self, *, destination_id: str | None, series: str, chapter: str) -> None:
        """Leave `coverage_unknown`: the watermark is given by hand. `series` is the id of the
        exported series (`status` prints it) or its name."""
        destination_id = self._destination(destination_id)
        self._refresh_names(destination_id)
        link, library = self._library()
        with self._comp.write_context() as w:
            pairing = self._pairing(w, destination_id)
            wanted = [
                p
                for p in pairing.propose(destination_id, library)
                if series in (p.export_series_id, p.series_name)
            ]
            if not wanted:
                raise UnknownSubjectError("no adopted series of this name in the destination")
            proposal = wanted[0]
            if proposal.state == "ambiguous" or not proposal.candidates:
                raise UnknownSubjectError("the upstream series is not chosen: `adopt pair` it")
            pairing.confirm(
                destination_id, proposal.export_series_id, proposal.candidates[0].ref,
                follow_from=chapter, confirm_backfill=None,
            )  # fmt: skip
        self._snapshot(link)

    @_refused
    def set_coverage(
        self, *, destination_id: str | None, series: str, volume: str, first: str, last: str
    ) -> int:
        """The chapters `first` to `last` are what the adopted volume `volume` of `series` holds
        (EF-BIB-044): they count as present, `backfill --gaps` asks for none of them. `series` is
        the id of the exported series (`status` prints it) or its name. Returns the chapters
        added."""
        self._require_not_restored()
        destination_id = self._destination(destination_id)
        self._refresh_names(destination_id)
        s, link = export.export_series, export.edition_export_series
        with self._comp.engine.begin() as connection:
            rows = connection.execute(
                sa.select(s.c.id, link.c.edition_id)
                .select_from(
                    s.join(
                        link,
                        sa.and_(
                            link.c.export_series_id == s.c.id,
                            link.c.remote_library_id == s.c.remote_library_id,
                        ),
                    )
                )
                .where(
                    s.c.shelf_destination_id == destination_id,
                    s.c.frozen_at.isnot(None),
                    sa.or_(s.c.id == series, s.c.name_raw == series, s.c.frozen_series == series),
                )
                .order_by(s.c.id)
            ).all()
            if not rows:
                raise UnknownSubjectError("no adopted series of this name in the destination")
            if len(rows) > 1:
                raise UnknownSubjectError("several series bear that name: give the id of one")
            row = rows[0]
            try:
                added = set_volume_coverage(connection, row.edition_id, volume, first, last)
            except LookupError as error:
                raise UnknownSubjectError(str(error)) from None
            except CoverageError as error:
                raise CodedRefusal("ops.invalid_choice", str(error)) from None
            flag_unknown_coverage(connection, row.id, row.edition_id)
        return added

    def _snapshot(self, _listed: Any = None) -> None:
        """The upstream settings, taken before anything is changed there (EF-EXP-056). The link
        that listed the library has closed its client: this one opens its own."""
        link = self._suwayomi()
        snapshots = UpstreamSnapshots(self._comp.engine, clock=self._comp.clock)

        async def take() -> None:
            try:
                await snapshots.take_missing(
                    link.service, link.fetcher, connected_service_id=SERVICE_ID
                )
                # Shelfhold has taken hold of the series: what it holds out of Suwayomi's updates
                # is written down, the only thing a detachment will remove
                await snapshots.record_exclusions(
                    link.service, link.fetcher, connected_service_id=SERVICE_ID
                )
            finally:
                await link.guarded.aclose()

        asyncio.run(take())

    # -- the switch-over (L30) -----------------------------------------------------------------

    @_refused
    def migration_report(self, destination_id: str | None) -> dict[str, Any]:
        destination_id = self._destination(destination_id)
        fsops, _loaded, _tombstones = self._comp.read_context()
        gate = self._gate(fsops, destination_id)
        report = gate.report(destination_id)
        return {
            "detected": report.detected,
            "fingerprint": report.fingerprint,
            "confirmed": gate.is_confirmed(destination_id),
            "steps": list(STEPS),
            "series": [asdict(f) for f in report.series],
        }

    @_refused
    def confirm_migration(self, *, destination_id: str | None, fingerprint: str) -> None:
        destination_id = self._destination(destination_id)
        fsops, _loaded, _tombstones = self._comp.read_context()
        gate = self._gate(fsops, destination_id)
        gate.confirm(destination_id, list(STEPS), fingerprint=fingerprint)

    # -- repatriation and detachment (L31) -----------------------------------------------------

    def _repatriator(self, w_fsops: Any, loaded: Any) -> Repatriator:
        return Repatriator(self._comp.engine, w_fsops, locate=loaded.locate, clock=self._comp.clock)

    @_refused
    def repatriate_plan(
        self, destination_id: str | None, source: str | None = None
    ) -> dict[str, Any]:
        destination_id = self._destination(destination_id)
        fsops, loaded, _tombstones = self._comp.read_context()
        return _plan(self._repatriator(fsops, loaded).plan(destination_id, source=source))

    def _kavita_target(self, destination_id: str) -> tuple[Any, str | None, str]:
        """The client of the destination's target, its library and the folder Kavita sees."""
        with self._comp.engine.connect() as connection:
            ctx = load_context(connection, destination_id)
            library = (
                None
                if ctx.remote_library_id is None
                else connection.execute(
                    sa.select(export.remote_library.c.remote_ref).where(
                        export.remote_library.c.id == ctx.remote_library_id
                    )
                ).scalar()
            )  # Kavita's own number of the library, not our identifier of the link
        if ctx.root_local is None:
            raise RepatriationBlockedError(())
        return (
            self._comp.kavita_client(ctx.target_id),
            None if library is None else str(library),
            ctx.remote_folder(ctx.root_local),
        )

    def _kavita_baseline(
        self, destination_id: str, repatriator: Repatriator, plan: RepatriationPlan
    ) -> None:
        """What Kavita says now, written down once, **before the first copy**. Refused (nothing
        copied) when it cannot be taken, when it does not cover the adopted files (an account that
        sees nothing, a scan still running), or when files are already home and there is no
        "before": a baseline taken after the damage would prove the damage is the reference. Once
        written it is never replaced."""
        store = BaselineStore(self._comp.engine)
        if store.load(destination_id) is not None:
            return
        if any(action.status == "present" for action in plan.files):
            raise RepatriationBlockedError(())  # copy begun without a "before"
        try:
            client, library_ref, remote_root = self._kavita_target(destination_id)
            if library_ref is None:
                raise RepatriationBlockedError(())
            snapshot = asyncio.run(read_snapshot(client, library_ref, remote_root))
        except _KAVITA_UNAVAILABLE:
            raise RepatriationBlockedError(()) from None
        series, rels = repatriator.adopted_paths(destination_id)
        if not covers(snapshot, remote_root, rels, series):
            raise RepatriationBlockedError(())
        store.save_if_absent(destination_id, snapshot)
        if store.load(destination_id) is None:  # a damaged value is in the way
            raise RepatriationBlockedError(())

    def _scan_allowed(self, destination_id: str) -> Callable[[str], bool]:
        """The sentinel's word before a scan (`require_scan_allowed`), for the folder of Kavita."""

        def allowed(remote: str) -> bool:
            try:
                with self._comp.engine.connect() as connection:
                    ctx = load_context(connection, destination_id)
                fsops, _loaded, _tombstones = self._comp.read_context()
                fsops.require_scan_allowed(ctx.path_map.to_local(remote))
            except (RootBlockedError, PathMapError, OSError, LookupError):
                _log.info(
                    "repatriate.scan_refused",
                    destination_id=destination_id,
                    reason="root_not_ready",
                )
                return False
            return True

        return allowed

    def _kavita_verdict(self, destination_id: str, repatriator: Repatriator) -> KavitaVerdict:
        """Scan the folder, read Kavita again, compare it with the baseline."""
        baseline = BaselineStore(self._comp.engine).load(destination_id)
        try:
            client, library_ref, remote_root = self._kavita_target(destination_id)
            if baseline is not None:
                series, rels = repatriator.adopted_paths(destination_id)
                if not covers(baseline, remote_root, rels, series):
                    return KavitaVerdict(reason="baseline_incomplete")
            return asyncio.run(
                verify(
                    client,
                    baseline,
                    library_ref,
                    remote_root,
                    unreachable=_KAVITA_UNAVAILABLE,
                    scan_allowed=self._scan_allowed(destination_id),
                )
            )
        except _KAVITA_UNAVAILABLE:
            return KavitaVerdict(reason="unreachable")

    @_refused
    def repatriate_apply(
        self, destination_id: str | None, *, fingerprint: str, source: str | None = None
    ) -> dict[str, Any]:
        destination_id = self._destination(destination_id)
        with self._comp.write_context() as w:
            repatriator = self._repatriator(w.fsops, w.loaded)
            plan = repatriator.plan(destination_id, source=source)
            if fingerprint == plan.fingerprint and not plan.frozen:
                self._kavita_baseline(destination_id, repatriator, plan)  # before any copy
            result = repatriator.apply(
                destination_id, fingerprint=fingerprint, job_id=new_ulid(), source=source
            )
            check = repatriator.check(
                destination_id, plan=plan, kavita=self._kavita_verdict(destination_id, repatriator)
            )
        return {
            "copied": result.copied,
            "already_present": result.already_present,
            "bytes_copied": result.bytes_copied,
            "check": _check(check),
        }

    @_refused
    def repatriate_check(self, destination_id: str | None) -> dict[str, Any]:
        """Run it again after the old mount is removed: Kavita is scanned and read, and compared
        with what it said before the copy."""
        destination_id = self._destination(destination_id)
        fsops, loaded, _tombstones = self._comp.read_context()
        repatriator = self._repatriator(fsops, loaded)
        check = repatriator.check(
            destination_id, kavita=self._kavita_verdict(destination_id, repatriator)
        )
        return _check(check)

    @_refused
    def give_back_upstream(
        self, *, shelf_id: str | None = None, edition_id: str | None = None
    ) -> dict[str, Any]:
        loaded = self._comp.load_roots()
        if loaded is not None and loaded.restore_pending:
            # the snapshots are those of a backup older than the disk: nothing is given back
            # to Suwayomi until `reconcile --adopt-after-restore` (EF-INT-026)
            raise RestoredBehindError(
                "restored_behind: run `shelfhold reconcile --adopt-after-restore`"
            )
        link = self._suwayomi()
        detacher = Detacher(
            self._comp.engine,
            UpstreamSnapshots(self._comp.engine, clock=self._comp.clock),
            connected_service_id=SERVICE_ID,
        )

        async def restore() -> Any:
            try:
                return await detacher.restore_upstream(
                    link.service, link.fetcher, DetachScope(shelf_id, edition_id)
                )
            finally:
                await link.guarded.aclose()

        report = asyncio.run(restore())
        return {
            "complete": report.complete,
            "series": [
                {
                    "binding_id": s.binding_id,
                    "restored_from": s.restored_from,
                    "ok": s.ok,
                    "error": s.error,
                    "note": s.note,
                }
                for s in report.series
            ],
            "managed": report.managed,
            "deleted_files": report.deleted_files,
        }

    # -- purge --now (L34) ---------------------------------------------------------------------

    @_refused
    def purge(
        self, *, kind: str, anchor_id: str, typed: str | None, principal: Principal
    ) -> dict[str, Any]:
        settings = self._comp.settings
        backups = None
        if settings.backup_dir is not None:
            folder, engine = settings.backup_dir, self._comp.engine

            def backups(since: datetime) -> list[tuple[str, datetime]]:
                sweep_orphans(settings.data_dir)  # a dead run's snapshot (the titles) is not listed
                return retained_since(engine, folder, since)

        with (
            self._comp.write_context() as w,
            connected_upstream(
                self._comp, w.fsops,
                IntentJournal(self._comp.engine, w.fsops, clock=self._comp.clock),
            ) as upstream,
        ):  # fmt: skip
            service = PurgeService(
                self._comp.engine, w.fsops, w.tombstones, clock=self._comp.clock,
                data_dir=settings.data_dir, bus=w.bus, fresh_auth_s=settings.fresh_auth_s,
                store_dir=settings.store_dir, upstream=upstream, backups=backups,
                index_lock=index_holder(self._comp),
            )  # fmt: skip
            run = service.purge_work if kind == "work" else service.purge_unit
            report: PurgeReport = run(
                anchor_id, principal=principal, typed=typed, now=self._comp.clock.now()
            )
        return {
            "anchor_id": report.anchor_id,
            "units": list(report.unit_ids),
            "complete": report.complete,
            "erased": report.erased,
            "unreachable_roots": list(report.unreachable_roots),
            "pending_roots": list(report.pending_roots),
            "refused": [vars(r) if hasattr(r, "__dict__") else str(r) for r in report.refused],
            "closed_tickets": report.closed_tickets,
            "upstream_released": report.upstream_released,
            "upstream_kept_by_others": report.upstream_kept_by_others,
            "unfollowed": report.unfollowed,
            "retained_backups": [
                {"name": b.name, "taken_at": b.taken_at.isoformat()}
                for b in report.retained_backups
            ],
            "alerts": list(report.alerts),
            "out_of_reach": list(report.out_of_reach),
        }

    # -- « Récupérer de l'espace » and the lift (M3 L26) -----------------------------------------

    @_refused
    def trash_empty(self, *, typed: str | None, principal: Principal) -> dict[str, Any]:
        """« Vider la corbeille maintenant » (reserved): the bins of every writable root, minus
        what the holds keep; the same method for the route and the host command."""
        return reclaim.trash_empty(self._comp, typed=typed, principal=principal)

    @_refused
    def tombstone_lift(self, *, seq: int, typed: str, principal: Principal) -> dict[str, Any]:
        """`tombstone lift` (reserved), on the tombstone service this process runs."""
        return reclaim.lift_tombstone(self._comp, seq=seq, typed=typed, principal=principal)

    @_refused
    def space_check(self, *, estimated_bytes: int) -> dict[str, Any]:
        """The « Ajouter » sheet against the free space less the reserve; nothing is written."""
        return reclaim.space_check(self._comp, estimated_bytes=estimated_bytes)

    # -- what the purges of M1a left in the service (L17): a plan, then a confirmation ------------

    @_refused
    def kept_ack(self, *, principal: Principal) -> dict[str, Any]:
        """The admin deleted in the service what a purge left there: the marks `status` shows go.
        A reserved action (the marks are the only reminder), with its trace in the audit log."""
        now = self._comp.clock.now()
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["purge.now"], now=now,
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):
            raise CodedRefusal("forbidden", type(verdict).__name__)
        setting = platform.app_setting
        with self._comp.engine.begin() as connection:
            count = connection.execute(
                sa.delete(setting).where(setting.c.key.like(KEPT_PREFIX + "%"))
            ).rowcount
            connection.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=principal.actor_id, action="purge.kept_acknowledged", at=now,
                        target=f"requests:{count}",
                    )
                )
            )  # fmt: skip
        return {"cleared": count}

    @_refused
    def settle_log(self) -> dict[str, Any]:
        """Only the emptying of the log, after a `purge.wal_busy`: no purge is run again."""
        truncate_wal(Path(str(self._comp.engine.url.database)))
        return {"done": True}

    @_refused
    def leftovers_plan(self) -> dict[str, Any]:
        """Read only. The migration to M1b offers; it never applies by itself."""
        with self._leftovers() as leftovers:
            if leftovers is None:
                return {"connected": False, "downloads": [], "dead_tickets": [], "unknown": 0,
                        "series": [], "series_unknown": False, "version": ""}  # fmt: skip
            plan = leftovers.plan()
        return {
            "connected": True,
            "downloads": [i.request_id for i in plan.downloads],
            "dead_tickets": [i.request_id for i in plan.dead_tickets],
            "unknown": plan.unknown,
            "series": [i.request_id for i in plan.series],
            "series_unknown": plan.series_unknown,
            "version": plan.version,
        }

    @_refused
    def leftovers_apply(self, *, if_match: str, principal: Principal) -> dict[str, Any]:
        with self._leftovers() as leftovers:
            if leftovers is None:
                raise CodedRefusal("source.not_connected", "no connected service")
            try:
                report = leftovers.apply(
                    if_match=if_match, principal=principal, now=self._comp.clock.now()
                )
            except StalePlanError as error:
                raise CodedRefusal("plan.stale", str(error)) from None
            except PermissionError:
                raise CodedRefusal("forbidden", "a fresh admin session is needed") from None
        return {
            "released": report.released,
            "kept_by_other_account": report.kept_by_other_account,
            "forgotten": report.forgotten,
            "unsettled": report.unsettled,
            "unfollowed": report.unfollowed,
        }

    @contextmanager
    def _leftovers(self) -> Iterator[PurgeLeftovers | None]:
        settings = self._comp.settings
        with (
            self._comp.write_context() as w,
            connected_upstream(
                self._comp, w.fsops,
                IntentJournal(self._comp.engine, w.fsops, clock=self._comp.clock),
            ) as upstream,
        ):  # fmt: skip
            yield (
                None if upstream is None else PurgeLeftovers(
                    self._comp.engine, upstream, clock=self._comp.clock,
                    fresh_auth_s=settings.fresh_auth_s, tombstones=w.tombstones,
                    compact=lambda: compact_database(
                        Path(str(self._comp.engine.url.database)),
                        MaintenanceGate(settings.data_dir / ".maintenance.lock"),
                    ),
                )
            )  # fmt: skip

    # -- the removal plan of the family shelves (L10, EF-BIB-037) --------------------------------

    @_refused
    def withdraw_plan(
        self, shelf_id: str | None = None, destination_id: str | None = None
    ) -> dict[str, Any]:
        """`destination_id`: the unrated files an adopted child library left readable (owner
        decision 2026-11-12), instead of what the family shelves refuse."""
        fsops, loaded, _tombstones = self._comp.read_context()
        service = WithdrawalService(
            self._comp.engine,
            fsops,
            clock=self._comp.clock,
            locate=loaded.locate,
            store_dir=str(self._comp.settings.store_dir),
        )
        try:
            plan = (
                service.adopted_unrated_plan(destination_id)
                if destination_id is not None
                else service.plan(shelf_id)
            )
        except LookupError:
            raise CodedRefusal("not_found", "no such destination") from None
        except WithdrawalRootReadOnlyError as error:
            raise CodedRefusal("shelf.withdrawal_root_read_only", str(error)) from None
        except NotChildLibraryError as error:
            raise CodedRefusal("shelf.not_child_library", str(error)) from None
        return _withdrawal_plan(plan)

    @_refused
    def withdraw_apply(
        self,
        *,
        if_match: str,
        principal: Principal,
        shelf_id: str | None = None,
        destination_id: str | None = None,
    ) -> dict[str, Any]:
        """Take out of the library of the children what the plan read lists, then ask Kavita to scan
        the folders the files left. The reserved check is the service's own."""
        asked: list[str] = []
        with self._comp.write_context() as w:
            service = WithdrawalService(
                self._comp.engine, w.fsops, clock=self._comp.clock, request_scan=asked.append,
                fresh_auth_s=self._comp.settings.fresh_auth_s,
                restricted_profile_exists=self._restricted_profile_exists,
                locate=w.loaded.locate, store_dir=str(self._comp.settings.store_dir),
            )  # fmt: skip
            try:
                report = service.apply(
                    if_match=if_match, principal=principal, now=self._comp.clock.now(),
                    shelf_id=shelf_id, destination_id=destination_id,
                )  # fmt: skip
            except WithdrawalRefusedError as error:
                raise CodedRefusal("forbidden", str(error)) from None
            except WithdrawalBusyError as error:
                raise CodedRefusal("job.busy", str(error)) from None
            except WithdrawalRootReadOnlyError as error:
                raise CodedRefusal("shelf.withdrawal_root_read_only", str(error)) from None
            except NotChildLibraryError as error:
                raise CodedRefusal("shelf.not_child_library", str(error)) from None
        self._scan_everywhere(asked)
        return {
            "withdrawn": list(report.withdrawn),
            "files": report.files,
            "pending": list(report.pending),
        }

    # -- deletion at three levels (L25, EF-BIB-021) ----------------------------------------------

    @_refused
    def delete_preview(self, *, level: str, unit_ids: list[str]) -> dict[str, Any]:
        """What the deletion would do, to the file. Reads only: nothing is written."""
        fsops, loaded, _tombstones = self._comp.read_context()
        service = DeletionService(
            self._comp.engine, fsops, clock=self._comp.clock, locate=loaded.locate
        )
        return _deletion_plan(service.preview(DeletionLevel(level), unit_ids))

    @_refused
    def delete_apply(
        self,
        *,
        level: str,
        unit_ids: list[str],
        if_match: str,
        typed: dict[str, str],
        principal: Principal,
    ) -> dict[str, Any]:
        """Applied for the version the admin read, and for nothing else (`plan.stale`). The
        reserved rule and the typed words are the service's own."""
        with self._comp.write_context() as w:
            service = DeletionService(
                self._comp.engine, w.fsops, clock=self._comp.clock, locate=w.loaded.locate,
                request_scan=lambda path: self._scan_everywhere([path]),
                refresh_index=self._index_refresher(w),
                fresh_auth_s=self._comp.settings.fresh_auth_s,
            )  # fmt: skip
            try:
                report = service.apply(
                    DeletionLevel(level), unit_ids, if_match=if_match, typed=typed,
                    principal=principal, now=self._comp.clock.now(),
                )  # fmt: skip
            except StalePlanError as error:
                raise CodedRefusal("plan.stale", str(error)) from None
        return {
            "level": report.level.value,
            "deletion_id": report.deletion_id,
            "units": list(report.units),
            "moved": report.moved,
            "pending": len(report.pending),
            "folders_removed": len(report.folders_removed),
            "folders_kept": len(report.folders_kept),
        }

    def _index_refresher(self, w: Any) -> Callable[[], object] | None:
        """`store/INDEX.jsonl` rewritten under the lock it shares with `purge --now`."""
        store = self._comp.settings.store_dir
        if store is None:
            return None
        index = StoreIndex(
            self._comp.engine, w.fsops, str(store), w.tombstones,
            hold=index_holder(self._comp),
        )  # fmt: skip
        return index.rewrite

    @_refused
    def delete_restore(
        self,
        *,
        unit_id: str,
        principal: Principal,
        deletion_id: str | None = None,
        partial: bool = False,
    ) -> dict[str, Any]:
        """Put back what ONE deletion binned, as it was (same path, same bytes). A file that
        cannot go back blocks only itself: refused as a whole, or `partial`."""
        with self._comp.write_context() as w:
            service = DeletionService(
                self._comp.engine, w.fsops, clock=self._comp.clock, locate=w.loaded.locate,
                refresh_index=self._index_refresher(w),
                fresh_auth_s=self._comp.settings.fresh_auth_s,
            )  # fmt: skip
            report = service.restore(
                unit_id, principal=principal, now=self._comp.clock.now(),
                deletion_id=deletion_id, partial=partial,
            )  # fmt: skip
        return {
            "unit_id": report.unit_id,
            "restored": report.restored,
            "left_in_bin": [{"path": p, "reason": r} for p, r in report.left_in_bin],
            "pending": len(report.pending),
        }

    # -- the erasure of the upstream download, per shelf (L16, EF-SRC-009) ------------------------

    @_refused
    def shelf_purge_upstream(
        self, *, shelf: str, enabled: bool, principal: Principal
    ) -> dict[str, Any]:
        """Turn on (or off) the erasure of the downloads of a shelf after a verified export. A
        reserved action, as it ends in a deletion nobody can bring back: a fresh admin session or
        the host's console, never a key."""
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["shelf.purge_upstream"], now=self._comp.clock.now(),
            fresh_auth_s=self._comp.settings.fresh_auth_s,
            restricted_profile_exists=self._restricted_profile_exists(),
        )  # fmt: skip
        if not isinstance(verdict, Allowed):
            raise CodedRefusal("forbidden", type(verdict).__name__)
        service = ShelfService(self._comp.engine, clock=self._comp.clock)
        try:
            shelf_id = service.shelf_id_of(shelf)
        except LookupError as error:
            raise UnknownSubjectError(str(error)) from None
        service.set_purge_upstream(shelf_id, enabled, actor=principal)
        return {"shelf_id": shelf_id, "enabled": enabled}

    @_refused
    def shelf_link_mode(
        self, *, shelf: str, link_mode: str, principal: Principal
    ) -> dict[str, Any]:
        """Choose how a kept copy of a shelf is made: `copy` or `hardlink` (EF-EXP-015). Not a
        deletion, so no reserved action; the writer refuses anything but those two."""
        service = ShelfService(self._comp.engine, clock=self._comp.clock)
        try:
            shelf_id = service.shelf_id_of(shelf)
        except LookupError as error:
            raise UnknownSubjectError(str(error)) from None
        try:
            service.set_link_mode(shelf_id, link_mode, actor=principal)
        except ValueError as error:
            raise CodedRefusal("validation_failed", str(error)) from None
        return {"shelf_id": shelf_id, "link_mode": link_mode}

    def _restricted_profile_exists(self) -> bool:
        """P3: a parental change asks a fresh password once a child profile exists."""
        with self._comp.engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(identity.user.c.id).where(identity.user.c.role == "child")
                ).first()
                is not None
            )

    def _scan_everywhere(self, remote_paths: list[str]) -> None:
        """Kavita forgets a series by scanning the folder: every Kavita target is asked (a scan of
        a folder a library does not watch is a no-op there)."""
        if not remote_paths:
            return
        table = export.export_target
        with self._comp.engine.connect() as connection:
            targets = list(
                connection.execute(sa.select(table.c.id).where(table.c.kind == "kavita")).scalars()
            )
        for target_id in targets:
            try:
                client = self._comp.kavita_client(str(target_id))
                for path in remote_paths:
                    asyncio.run(client.scan_folder(path))
            except _KAVITA_UNAVAILABLE:
                _log.warning("withdraw.scan_unavailable", target_id=str(target_id))

    # -- the write probe (L37, EF-UX-002) ------------------------------------------------------

    @_refused
    def probe(self, folder: str) -> dict[str, Any]:
        with self._comp.write_context() as w:
            report = probe_folder(w.fsops, folder)  # type: ignore[arg-type]
        return {
            "ok": report.ok,
            "folder": report.folder,
            "steps": list(report.steps),
            "failed_step": report.failed_step,
            "cause": report.cause,
            "event": report.event,
            "yaml_hint": report.yaml_hint,
        }
