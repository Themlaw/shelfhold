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
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa

from shelfhold.acquisition.application.upstream import (
    Detacher,
    DetachScope,
    UpstreamSnapshots,
)
from shelfhold.acquisition.public import decline_pairing
from shelfhold.composition_errors import KeyringUnavailableError, NoLibraryError
from shelfhold.export.application.coverage import (
    CoverageError,
    flag_unknown_coverage,
    set_volume_coverage,
)
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
from shelfhold.export.application.preflight import PermissionPreflight
from shelfhold.export.application.purge import (
    PurgeCollateralError,
    PurgeRefusedError,
    PurgeReport,
    PurgeService,
    PurgeUnknownError,
)
from shelfhold.export.application.repatriate import (
    RepatriationCheck,
    RepatriationPlan,
    Repatriator,
)
from shelfhold.export.application.restore import RestoredBehindError
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.series import refresh_names
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.fsops.errors import RootBlockedError
from shelfhold.infrastructure.fsops.probe import probe_folder
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import ConfirmationRequiredError
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError, OpsError, UnknownSubjectError
from shelfhold.sdk.refs import SeriesRef

if TYPE_CHECKING:
    from shelfhold.composition import Composition, WriteContext

__all__ = ["LibraryOps"]

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


def _check(check: RepatriationCheck) -> dict[str, Any]:
    return {
        "series": check.series,
        "chapters": check.chapters,
        "files": check.files,
        "missing": list(check.missing),
        "different": list(check.different),
        "safe_to_unmount": check.safe_to_unmount,
    }


OWN_CODES = frozenset({"pairing", "repatriate", "backfill", "instance"})
# exceptions of the services that carry no catalog code of their own: (class, code)
_CODED: tuple[tuple[type[Exception], str], ...] = (
    (PurgeUnknownError, "ops.unknown_subject"),
    (PurgeCollateralError, "purge.collateral"),
    (ConfirmationRequiredError, "unit.confirmation_required"),
    (PurgeRefusedError, "forbidden"),
    (SwitchoverIncompleteError, "pairing.switchover_unconfirmed"),
    (NoLibraryError, "ops.exit_unavailable"),
    (KeyringUnavailableError, "ops.exit_unavailable"),
)


def refusal_of(error: Exception) -> OpsError | None:
    """The refusal that stands for a service's exception, or None when it is a real failure."""
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
        if destination_id:
            return destination_id
        table = export.shelf_destination
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

    def _refresh_names(self, destination_id: str) -> None:
        """Kavita may have renamed a series: the names the pairing compares are the current ones
        (EF-EXP-057). A Kavita that cannot be asked leaves the cache as it is."""
        table = export.shelf_destination
        try:
            with self._comp.engine.connect() as connection:
                target_id = connection.execute(
                    sa.select(table.c.target_id).where(table.c.id == destination_id)
                ).scalar_one()
            client = self._comp.kavita_client(str(target_id))
            asyncio.run(refresh_names(self._comp.engine, client, destination_id))
        except (KeyringUnavailableError, TargetError, KeyError, sa.exc.NoResultFound):
            return

    def _pairing(self, w: WriteContext) -> Pairing:
        detector = SwitchoverDetector(self._comp.engine, w.fsops)
        return Pairing(
            self._comp.engine,
            PermissionPreflight(self._comp.engine, w.fsops, w.bus, clock=self._comp.clock),
            SwitchoverGate(self._comp.engine, detector, clock=self._comp.clock),
            clock=self._comp.clock,
            tombstones=w.tombstones,
            translation_nature=self._nature,
        )

    # -- pairing (L30) -------------------------------------------------------------------------

    @_refused
    def pair_propose(self, destination_id: str | None) -> list[dict[str, Any]]:
        destination_id = self._destination(destination_id)
        self._refresh_names(destination_id)
        _link, library = self._library()
        with self._comp.write_context() as w:
            return [_proposal(p) for p in self._pairing(w).propose(destination_id, library)]

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
            result = self._pairing(w).confirm(
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
            pairing = self._pairing(w)
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
            pairing = self._pairing(w)
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

    def _snapshot(self, link: Any) -> None:
        """The upstream settings, taken before anything is changed there (EF-EXP-056)."""
        snapshots = UpstreamSnapshots(self._comp.engine, clock=self._comp.clock)

        async def take() -> None:
            try:
                await snapshots.take_missing(
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
        gate = SwitchoverGate(
            self._comp.engine, SwitchoverDetector(self._comp.engine, fsops), clock=self._comp.clock
        )
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
        gate = SwitchoverGate(
            self._comp.engine, SwitchoverDetector(self._comp.engine, fsops), clock=self._comp.clock
        )
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

    @_refused
    def repatriate_apply(
        self, destination_id: str | None, *, fingerprint: str, source: str | None = None
    ) -> dict[str, Any]:
        destination_id = self._destination(destination_id)
        with self._comp.write_context() as w:
            repatriator = self._repatriator(w.fsops, w.loaded)
            plan = repatriator.plan(destination_id, source=source)
            result = repatriator.apply(
                destination_id, fingerprint=fingerprint, job_id=new_ulid(), source=source
            )
            check = repatriator.check(destination_id, plan=plan)
        return {
            "copied": result.copied,
            "already_present": result.already_present,
            "bytes_copied": result.bytes_copied,
            "check": _check(check),
        }

    @_refused
    def repatriate_check(self, destination_id: str | None) -> dict[str, Any]:
        destination_id = self._destination(destination_id)
        fsops, loaded, _tombstones = self._comp.read_context()
        return _check(self._repatriator(fsops, loaded).check(destination_id))

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
        with self._comp.write_context() as w:
            service = PurgeService(
                self._comp.engine, w.fsops, w.tombstones, clock=self._comp.clock,
                data_dir=self._comp.settings.data_dir, bus=w.bus,
                fresh_auth_s=self._comp.settings.fresh_auth_s,
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
            "alerts": list(report.alerts),
            "out_of_reach": list(report.out_of_reach),
        }

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
