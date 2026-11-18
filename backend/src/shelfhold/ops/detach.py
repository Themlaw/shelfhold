"""`target remove` and `detach --full` over the services of the export context (M3 L30,
EF-EXP-031): the preview that says what would be lost, the removal of a destination (keep the
files or take them away) and the full detachment of a Kavita target.

Both are reserved actions (the service checks them again, here and not only at the door): a fresh
admin session or the host CLI, never a key, a member or a child, with the phrase typed. The
refusals leave as catalog codes (`removal.refused`, `detach.refused`) whose message is a closed
word, never a path or a name.

The roots are read when a command runs (`target add` registers one while the server runs). The
exported files are never opened here: `Fsops` is the only door to the disk, the journal the only
way to a deletion.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from shelfhold.export.application.detach import (
    DETACH_JOB_KIND,
    DetachRefusedError,
    DetachReport,
    FullDetacher,
    detach_job_key,
    detach_order_of,
)
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.pending_scans import remember_scans
from shelfhold.export.application.remove_destination import (
    REMOVAL_JOB_KIND,
    RemovalMode,
    RemovalRefusedError,
    RemovalReport,
    RemoveDestination,
    removal_job_key,
    removal_order_of,
)
from shelfhold.export.application.root_ref import RootRef
from shelfhold.export.application.target_state import load_fingerprint
from shelfhold.export.domain.progress_sync import can_sync_progress
from shelfhold.export.targets.kavita.account_session import KavitaAccountSession
from shelfhold.export.targets.kavita.collections import CollectionsSync
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.settings_restore import restore_library_settings
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.jobs.public import MAX_ATTEMPTS, JobContext, JobFailedError
from shelfhold.library.public import roots_holding_library_copies
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError, InvalidChoiceError
from shelfhold.ops.events import BusTargetEvents

if TYPE_CHECKING:
    from shelfhold.composition import Composition
    from shelfhold.export.application.roots import LoadedRoots
    from shelfhold.ops.ports import HeadlessOps

__all__ = ["DetachOps", "register_teardown_jobs"]

log = structlog.get_logger()

_MODES = {m.value: m for m in RemovalMode}


class _Revoker:
    """The account's door: Kavita revokes the key for a login, so the password comes with it."""

    def __init__(self, headless: HeadlessOps) -> None:
        self._headless = headless

    def revoke(self, target_id: str, *, credential: str | None) -> bool:
        key = self.key_id(target_id, credential=credential)
        return key is not None and (
            self.revoke_id(target_id, credential=credential, key_id=key) == "revoked"
        )

    def key_id(self, target_id: str, *, credential: str | None) -> str | None:
        return self._headless.target_find_key(target=target_id, password=credential)

    def revoke_id(self, target_id: str, *, credential: str | None, key_id: str) -> str:
        return self._headless.target_revoke_key_id(
            target=target_id, password=credential, key_id=key_id
        )


class DetachOps:
    def __init__(
        self,
        comp: Composition,
        headless: HeadlessOps,
        *,
        flagged_names: Callable[[str], int] | None = None,
    ) -> None:
        self._comp = comp
        self._headless = headless
        self._flagged_names = flagged_names

    # -- the pieces -----------------------------------------------------------------------------

    def _context(self) -> tuple[Fsops, LoadedRoots, IntentJournal]:
        fsops, loaded, _tombstones = self._comp.read_context()
        return fsops, loaded, IntentJournal(self._comp.engine, fsops, clock=self._comp.clock)

    @staticmethod
    def _root_directory(loaded: LoadedRoots) -> Callable[[str], RootRef | None]:
        rows = {r.root_id: r for r in loaded.roots}

        def roots(local: str) -> RootRef | None:
            root_id = loaded.root_id_for(local)
            return None if root_id is None else RootRef(root_id, rows[root_id].path)

        return roots

    def _remover(
        self,
        fsops: Fsops,
        loaded: LoadedRoots,
        journal: IntentJournal,
        scan: Callable[[str], None] | None,
    ) -> RemoveDestination:
        comp = self._comp
        return RemoveDestination(
            comp.engine, fsops, journal, clock=comp.clock, roots=self._root_directory(loaded),
            locks=ResourceLocks(comp.engine, clock=comp.clock), request_scan=scan,
            fresh_auth_s=comp.settings.fresh_auth_s,
        )  # fmt: skip

    def _scan_of(self, destination_id: str) -> Callable[[str], None] | None:
        """One scan of the folder once its files are gone: best effort, Kavita may be asleep."""
        import sqlalchemy as sa

        from shelfhold.infrastructure.schema import export

        with self._comp.engine.connect() as conn:
            target = conn.execute(
                sa.select(export.shelf_destination.c.target_id).where(
                    export.shelf_destination.c.id == destination_id
                )
            ).scalar()
        if target is None or str(target) not in self._comp.kavita_targets():
            return None
        client = self._comp.kavita_client(str(target))

        def scan(remote_folder: str) -> None:
            try:
                asyncio.run(client.scan_folder(remote_folder))
            except TargetError:  # not lost: asked again by the next export run
                remember_scans(self._comp.engine, [remote_folder])

        return scan

    # -- list the destinations ------------------------------------------------------------------

    # Lives here, not with the shelves, because the screens' fixed tests read it on `teardown`.
    def list_destinations(self) -> list[dict[str, Any]]:
        import json

        import sqlalchemy as sa

        from shelfhold.infrastructure.schema import export

        shelf = export.shelf
        destination = export.shelf_destination
        target = export.export_target
        with self._comp.engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    destination.c.id,
                    destination.c.shelf_id,
                    shelf.c.name.label("shelf_name"),
                    destination.c.target_id,
                    destination.c.root_subpath,
                    destination.c.transport,
                    destination.c.adopted,
                    destination.c.template,
                    destination.c.sync_progress,
                    target.c.kind.label("target_kind"),
                )
                .select_from(
                    destination.join(shelf, destination.c.shelf_id == shelf.c.id).join(
                        target, destination.c.target_id == target.c.id
                    )
                )
                .order_by(destination.c.id)
            ).all()
        count = self._flagged_names
        if rows and count is None:
            raise RuntimeError("the count of contested names is not wired")
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                template = json.loads(row.template)
            except (TypeError, ValueError):
                template = {}
            flagged: int | None = None
            reason: str | None = None
            if count is not None:
                try:
                    flagged = count(row.id)
                except Exception as error:  # one destination that cannot be counted is still listed
                    reason = type(error).__name__
                    log.warning(
                        "destination.flagged_names_failed", destination_id=row.id, reason=reason
                    )
            items.append(
                {
                    "id": row.id,
                    "shelf_id": row.shelf_id,
                    "shelf_name": row.shelf_name,
                    "target_id": row.target_id,
                    "root_subpath": row.root_subpath,
                    "transport": row.transport,
                    "adopted": bool(row.adopted),
                    "flagged_names": flagged,
                    "flagged_reason": reason,  # why there is no count: the screen says it
                    "template": template,
                    "target_kind": row.target_kind,
                    "sync_progress": bool(row.sync_progress),  # M4 L46: the switch of the binding
                    "can_sync_progress": can_sync_progress(row.target_kind),
                }
            )
        return items

    # -- remove a destination -------------------------------------------------------------------

    def remove_preview(self, *, destination_id: str, mode: str) -> dict[str, Any]:
        fsops, loaded, journal = self._context()
        try:
            preview = self._remover(fsops, loaded, journal, None).preview(
                destination_id, _mode(mode)
            )
        except RemovalRefusedError as error:
            raise _removal(error) from None
        return {
            "destination_id": preview.destination_id, "mode": preview.mode.value,
            "phrase": preview.phrase,
            "warnings": [{"code": w.code, "count": w.count} for w in preview.warnings],
        }  # fmt: skip

    def remove(
        self, *, destination_id: str, mode: str, typed: str | None, give_up: bool,
        principal: Principal,
    ) -> dict[str, Any]:  # fmt: skip
        fsops, loaded, journal = self._context()
        service = self._remover(fsops, loaded, journal, self._scan_of(destination_id))
        try:
            if give_up:
                report = service.settle(destination_id, resume=False, principal=principal)
            else:
                report = service.apply(
                    destination_id, _mode(mode), principal=principal, typed=typed
                )
        except RemovalRefusedError as error:
            raise _removal(error) from None
        return _removal_report(report)

    def remove_queue(
        self, *, destination_id: str, mode: str, typed: str | None, principal: Principal
    ) -> str:
        """The order of a removal that takes the files, as a job (M4 L34). The reserved action
        and the phrase are checked here, now, and the audit line is written; nothing is deleted.
        The job runs from the identifiers in its key, and the same order while it is active is the
        same job. Returns its id."""
        fsops, loaded, journal = self._context()
        service = self._remover(fsops, loaded, journal, None)
        try:
            order = service.order(
                destination_id, _mode(mode), principal=principal, typed=typed, record=False
            )
        except RemovalRefusedError as error:
            raise _removal(error) from None
        pools = self._pools()  # a refusal comes first; no line is written for a job never queued
        service.record_order(order, principal)
        job_id: str = pools.submit(
            REMOVAL_JOB_KIND, dedupe_key=removal_job_key(order), priority_class="request"
        ).job_id
        return job_id

    def _pools(self) -> Any:
        pools = self._comp.pools()
        if pools is None:
            raise ExitUnavailableError("the job queue is not running")
        return pools

    def run_removal_job(self, context: JobContext) -> None:
        """The handler of `destination.remove`: what the order in the key says, again after a
        stop. A destination held by another job, or a file operation that is open, is tried again
        later; anything left is a failed job, said by its code."""
        try:
            order = removal_order_of(context.dedupe_key)
        except ValueError:
            raise JobFailedError("job.failed") from None
        fsops, loaded, journal = self._context()
        service = self._remover(fsops, loaded, journal, self._scan_of(order.destination_id))
        try:
            report = service.execute(order, job_id=context.job_id)
        except RemovalRefusedError as error:
            if error.reason in _TRANSIENT:
                raise _Busy(error.reason) from None
            raise JobFailedError("removal.refused") from None
        context.record(
            deleted=report.deleted, forgotten=report.forgotten, left=len(report.left),
            complete=int(report.complete),
        )  # fmt: skip
        if not report.complete:
            raise JobFailedError("removal.incomplete")

    # -- detach a target ------------------------------------------------------------------------

    def _full_detacher(
        self, fsops: Fsops, loaded: LoadedRoots, journal: IntentJournal
    ) -> FullDetacher:
        comp = self._comp

        def library_roots() -> set[str]:
            with comp.engine.connect() as conn:
                return roots_holding_library_copies(conn)

        return FullDetacher(
            comp.engine, fsops, journal, clock=comp.clock, roots=self._root_directory(loaded),
            restore=restore_library_settings, library_roots=library_roots,
            locks=ResourceLocks(comp.engine, clock=comp.clock),
            store_path=str(comp.settings.store_dir), run=comp.run,
            fresh_auth_s=comp.settings.fresh_auth_s,
        )  # fmt: skip

    def _account_session(self, target_id: str, password: str | None) -> KavitaAccountSession | None:
        """The admin account's session, when its password came with the command."""
        import sqlalchemy as sa

        from shelfhold.infrastructure.schema import export

        if not password:
            return None
        with self._comp.engine.connect() as conn:
            config = conn.execute(
                sa.select(export.export_target.c.config).where(
                    export.export_target.c.id == target_id
                )
            ).scalar_one()
        url = str(config["url"])
        with self._comp.engine.connect() as conn:
            stored = load_fingerprint(conn, target_id)
        return KavitaAccountSession(
            self._comp.network.integration_fetcher(url),
            url,
            str(config.get("username", "")),
            password,
            stored_fingerprint=stored,
        )

    def full_preview(self, *, target_id: str) -> dict[str, Any]:
        fsops, loaded, journal = self._context()
        try:
            preview = self._full_detacher(fsops, loaded, journal).preview(target_id)
        except DetachRefusedError as error:
            raise _full_refusal(error) from None
        return {
            "target_id": preview.target_id, "phrase": preview.phrase,
            "warnings": [{"code": w.code, "count": w.count} for w in preview.warnings],
        }  # fmt: skip

    def _withdraw_pushed(self, target_id: str, client: Any) -> None:
        """Before the key goes (the detacher calls it once authorization and phrase have passed):
        take back the collections and lists Shelfhold pushed, a promoted object would outlive the
        destination. Best effort, said by the log: what cannot be reached stays for the person."""
        sync = CollectionsSync(
            self._comp.engine, self._comp.clock, BusTargetEvents(self._comp.engine, self._comp.bus)
        )
        try:
            self._comp.run(sync.withdraw_all(client, target_id))
        except Exception as error:
            log.warning("detach.collections_failed", error=type(error).__name__)

    def full_queue(
        self, *, target_id: str, typed: str | None, credential: str | None,
        discard_hidden_data: bool, discard_tombstone_journal: bool, key_gone: bool = False,
        principal: Principal,
    ) -> str:  # fmt: skip
        """The order of a full detachment, as a job (M4 L34). The checks and the audit line are
        now; the password goes to memory under the key of the job, never to the queue, for one
        run. Returns the id of the job."""
        fsops, loaded, journal = self._context()
        detacher = self._full_detacher(fsops, loaded, journal)
        try:
            order = detacher.order(
                target_id, principal=principal, typed=typed, key_gone=key_gone,
                credential_given=bool(credential), discard_hidden_data=discard_hidden_data,
                discard_tombstone_journal=discard_tombstone_journal, record=False,
            )  # fmt: skip
        except DetachRefusedError as error:
            raise _full_refusal(error) from None
        pools = self._pools()  # a refusal comes first; no line is written for a job never queued
        detacher.record_order(order, principal)
        key = detach_job_key(order)
        handoff = self._comp.credential_handoff
        waiting = handoff.holds(key)  # an earlier order of the same job, not yet taken
        if credential:
            handoff.put(key, credential)
        try:
            created = pools.submit(DETACH_JOB_KIND, dedupe_key=key, priority_class="request")
        except BaseException:
            if not waiting:
                handoff.discard(key)  # no job will come for it
            raise
        if not created.created and not waiting:
            handoff.discard(key)  # the job in flight took its password already: this one is no use
        job_id: str = created.job_id
        return job_id

    def run_detach_job(self, context: JobContext) -> None:
        """The handler of `target.detach`: the order in the key, with the password the door left
        in memory if it is still there. Without it the steps that need none are done, the key
        stays and the job fails `detach.incomplete`: the same order, given again, finishes it."""
        try:
            order = detach_order_of(context.dedupe_key)
        except ValueError:
            raise JobFailedError("job.failed") from None
        handoff = self._comp.credential_handoff
        credential = handoff.take(context.dedupe_key)
        fsops, loaded, journal = self._context()
        detacher = self._full_detacher(fsops, loaded, journal)
        target_id = order.target_id
        known = target_id in self._comp.kavita_targets()
        client = self._comp.kavita_client(target_id) if known else None
        account = self._account_session(target_id, credential) if order.key_gone and known else None
        try:
            report = detacher.execute(
                order, client=client, revoker=_Revoker(self._headless), credential=credential,
                account=account, job_id=context.job_id,
                before_restore=(
                    None if client is None else lambda: self._withdraw_pushed(target_id, client)
                ),
            )  # fmt: skip
        except DetachRefusedError as error:
            if error.reason in _TRANSIENT and credential and _attempts_left(context):
                handoff.put(context.dedupe_key, credential)  # the next attempt needs it again
            if error.reason in _TRANSIENT:
                raise _Busy(error.reason) from None
            if error.reason == "credential_required":
                raise JobFailedError("detach.incomplete") from None
            raise JobFailedError("detach.refused") from None
        finally:
            if account is not None:
                with contextlib.suppress(Exception):  # the connection is best-effort closed
                    self._comp.run(account.aclose())
        self._comp.roots_changed()  # the roots demoted are no longer expected stamped
        context.record(
            complete=int(report.complete), hidden_removed=len(report.hidden_removed),
            key_revoked=int(report.key_revoked), forgotten=report.forgotten,
        )  # fmt: skip
        if not report.complete:
            raise JobFailedError("detach.incomplete")

    def full(
        self, *, target_id: str, typed: str | None, credential: str | None,
        discard_hidden_data: bool, discard_tombstone_journal: bool, key_gone: bool = False,
        give_up: bool, principal: Principal,
    ) -> dict[str, Any]:  # fmt: skip
        fsops, loaded, journal = self._context()
        detacher = self._full_detacher(fsops, loaded, journal)
        known = target_id in self._comp.kavita_targets()
        client = self._comp.kavita_client(target_id) if known else None
        account = self._account_session(target_id, credential) if key_gone and known else None
        try:
            if give_up:
                report = detacher.settle(target_id, resume=False, principal=principal)
            else:
                report = detacher.apply(
                    target_id, principal=principal, typed=typed, client=client,
                    revoker=_Revoker(self._headless), credential=credential,
                    discard_hidden_data=discard_hidden_data,
                    discard_tombstone_journal=discard_tombstone_journal,
                    key_gone=key_gone, account=account,
                    before_restore=(
                        None if client is None
                        else lambda: self._withdraw_pushed(target_id, client)
                    ),
                )  # fmt: skip
        except DetachRefusedError as error:
            raise _full_refusal(error) from None
        finally:
            if account is not None:
                with contextlib.suppress(Exception):  # the connection is best-effort closed
                    self._comp.run(account.aclose())
        if report.past_no_return:  # nothing was undone, and the report must not say it was
            raise CodedRefusal("detach.refused", "past_no_return")
        self._comp.roots_changed()  # the roots demoted are no longer expected stamped
        return _full_report(report)


_TRANSIENT = frozenset({"busy", "intent_open"})  # another job holds it, or an operation is open


def _attempts_left(context: JobContext) -> bool:
    """Whether the queue will run this job again: on its last attempt the password is not kept
    for a run that will not come (the job becomes a dead letter)."""
    return bool(context.ledger.attempts_of(context.job_id) < MAX_ATTEMPTS)


class _Busy(Exception):
    """The destination or the target is held by another job just now: the queue tries again
    after its backoff (an attempt is spent, nothing else)."""


def register_teardown_jobs(pools: Any, comp: Composition, headless: HeadlessOps) -> None:
    """The two kinds of job of M4 L34. Both are write jobs: a pause of the queue parks them and
    nothing is deleted until it ends."""
    ops = DetachOps(comp, headless)
    pools.register(REMOVAL_JOB_KIND, pool="io", handler=ops.run_removal_job)
    pools.register(DETACH_JOB_KIND, pool="io", handler=ops.run_detach_job)


def _mode(value: str) -> RemovalMode:
    if value not in _MODES:
        raise InvalidChoiceError("choose `keep_files` or `delete_files`")
    return _MODES[value]


def _removal(error: RemovalRefusedError) -> CodedRefusal:
    if error.reason == "not_allowed":
        return CodedRefusal("forbidden", type(error.verdict).__name__)
    return CodedRefusal("removal.refused", error.reason)


def _full_refusal(error: DetachRefusedError) -> CodedRefusal:
    if error.reason == "not_allowed":
        return CodedRefusal("forbidden", type(error.verdict).__name__)
    return CodedRefusal("detach.refused", error.reason)


def _removal_report(report: RemovalReport) -> dict[str, Any]:
    return {
        "deleted": report.deleted, "forgotten": report.forgotten, "cancelled": report.cancelled,
        "complete": report.complete,
        "left": [{"code": line.code, "path": line.path} for line in report.left],
    }  # fmt: skip


def _full_report(report: DetachReport) -> dict[str, Any]:
    return {
        "target_id": report.target_id, "complete": report.complete,
        "hidden_removed": len(report.hidden_removed), "key_revoked": report.key_revoked,
        "key_gone": report.key_gone,
        "forgotten": report.forgotten, "cancelled": report.cancelled,
        "settings_restored": any(s.exclude_patterns_restored for s in report.settings),
        "settings": [
            {
                "exclude_patterns_restored": s.exclude_patterns_restored,
                "type_unchanged": s.type_unchanged,
            }
            for s in report.settings
        ],
        "leftovers": [{"code": line.code, "count": line.count} for line in report.leftovers],
    }  # fmt: skip
