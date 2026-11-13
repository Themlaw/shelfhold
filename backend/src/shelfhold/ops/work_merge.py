"""Merging Works, « Séparer », and the plan that moves an exported series (M3 L22: EF-BIB-019,
EF-EXP-062, EF-BIB-049).

Three reserved actions, each a fresh admin session or the host CLI, never a key, never a member or
a child (`work.merge`, `work.separate`, `series.reassign`). A merge and a series reassignment show
their plan first; they act only for the fingerprint of that plan, recomputed under the write lock,
and write their audit row in the transaction that moves the rows. « Séparer » undoes a merge
within 30 days (`merge_log`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import Connection, exc

from shelfhold.catalog.public import (
    Catalog,
    MergeAlreadyUndoneError,
    MergeExpiredError,
    SeparationBlockedError,
    SeriesAlreadyAttachedError,
    StaleMergePlanError,
    WorkMergePlan,
    WorkMergeRefusedError,
    apply_work_merge,
    plan_work_merge,
    separate_works,
)
from shelfhold.export.public import (
    SeriesReassignmentPlan,
    SeriesReassignmentRefusedError,
    apply_series_reassignment,
    plan_series_reassignment,
)
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    Credential,
    Principal,
    authorize_reserved,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity
from shelfhold.ops.errors import CodedRefusal, StalePreviewError, UnknownSubjectError

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["WorkMergeOps"]


def _merge_report(plan: WorkMergePlan, applied: bool, merge_id: str | None) -> dict[str, Any]:
    return {
        "keep_id": plan.keep_id, "drop_id": plan.drop_id, "editions": list(plan.editions),
        "blockers": list(plan.blockers), "keep_age": plan.keep_age, "drop_age": plan.drop_age,
        "merged_age": plan.merged_age, "unrated_after": plan.unrated_after,
        "mergeable": plan.mergeable, "fingerprint": plan.fingerprint, "applied": applied,
        "merge_id": merge_id,
    }  # fmt: skip


def _series_report(plan: SeriesReassignmentPlan, applied: bool) -> dict[str, Any]:
    return {
        "work_id": plan.work_id,
        "moves": [
            {"edition_id": m.edition_id, "remote_library_id": m.remote_library_id,
             "from_series_id": m.from_series_id, "to_series_id": m.to_series_id}
            for m in plan.moves
        ],
        "blockers": list(plan.blockers), "path_changes": plan.path_changes,
        "emptied": list(plan.emptied), "mergeable": plan.mergeable,
        "fingerprint": plan.fingerprint, "applied": applied,
    }  # fmt: skip


class WorkMergeOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def _authorize(self, principal: Principal, action: str) -> None:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS[action], now=self._comp.clock.now(),
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the socket decided first; this one is the last gate
            raise CodedRefusal("forbidden", type(verdict).__name__)

    def _audit(
        self,
        connection: Connection,
        principal: Principal,
        action: str,
        target: str,
        *,
        always: bool = False,
    ) -> None:
        if principal.credential is Credential.SESSION and not always:
            return  # the route's guard wrote the line for this session
        connection.execute(
            sa.insert(identity.audit).values(
                id=new_ulid(), actor_id=principal.actor_id, action=action, target=target,
                session_id=principal.session_id, auth_time=principal.auth_time,
            )
        )  # fmt: skip

    def merge(
        self, keep_id: str, drop_id: str, *, confirm: str | None, principal: Principal
    ) -> dict[str, Any]:
        """The plan to merge `drop_id` into `keep_id`; applied too when `confirm` is its
        fingerprint."""
        self._authorize(principal, "work.merge")
        engine = self._comp.engine
        try:
            if confirm is None:
                with engine.connect() as connection:
                    return _merge_report(plan_work_merge(connection, keep_id, drop_id), False, None)
            with engine.connect() as connection:
                connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
                with connection.begin():
                    done = apply_work_merge(
                        connection, keep_id, drop_id, confirm=confirm, now=self._comp.clock.now()
                    )
                    self._audit(
                        connection, principal, "work.merge",
                        f"{keep_id};{drop_id};fp:{done.plan.fingerprint};merge:{done.merge_id}",
                    )  # fmt: skip
            return _merge_report(done.plan, True, done.merge_id)
        except LookupError as error:
            raise UnknownSubjectError(str(error)) from None
        except ValueError as error:
            raise CodedRefusal("work.merge_refused", str(error)) from None
        except StaleMergePlanError as error:
            raise StalePreviewError(str(error)) from None
        except WorkMergeRefusedError as error:
            raise CodedRefusal("work.merge_refused", ", ".join(error.plan.blockers)) from None

    def separate(self, merge_id: str, *, principal: Principal) -> dict[str, Any]:
        """« Séparer »: put the two Works of a merge back, within 30 days."""
        self._authorize(principal, "work.separate")
        try:
            with self._comp.engine.connect() as connection:
                connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
                with connection.begin():
                    done = separate_works(connection, merge_id, now=self._comp.clock.now())
                    self._audit(
                        connection, principal, "work.separate",
                        f"{done.survivor_id};{done.restored_id};merge:{merge_id}",
                    )  # fmt: skip
        except LookupError as error:
            raise UnknownSubjectError(str(error)) from None
        except (MergeExpiredError, MergeAlreadyUndoneError, SeparationBlockedError) as error:
            raise CodedRefusal(error.code, merge_id) from None
        return {
            "merge_id": merge_id, "survivor_id": done.survivor_id, "restored_id": done.restored_id,
            "shelves": done.shelves, "waiting": done.waiting,
        }  # fmt: skip

    def series_reassign(
        self, work_id: str, *, confirm: str | None, principal: Principal
    ) -> dict[str, Any]:
        """The plan that moves the exported series of a Work's Editions; applied only for the
        fingerprint of the plan the admin read."""
        self._authorize(principal, "series.reassign")
        engine = self._comp.engine
        try:
            if confirm is None:
                with engine.connect() as connection:
                    return _series_report(plan_series_reassignment(connection, work_id), False)
            with engine.connect() as connection:
                connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
                with connection.begin():
                    plan = apply_series_reassignment(
                        connection, work_id, confirm=confirm, now=self._comp.clock.now()
                    )
                    self._audit(
                        connection, principal, "series.reassign",
                        f"{work_id};fp:{plan.fingerprint};moves:{len(plan.moves)}",
                    )  # fmt: skip
            return _series_report(plan, True)
        except SeriesReassignmentRefusedError as error:
            if error.reasons == ("plan.stale",):
                raise StalePreviewError(work_id) from None
            raise CodedRefusal(error.code, ", ".join(error.reasons)) from None

    def attach_series(
        self, work_id: str, series_id: str, position: str, *, principal: Principal
    ) -> dict[str, Any]:
        """A Series that comes after its Work (EF-BIB-025): only the membership is written. What an
        exported series becomes is the plan returned with it, which nothing here applies
        (EF-EXP-062). For an admin; the audit row names no title."""
        try:
            with self._comp.engine.connect() as connection:
                connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
                with connection.begin():
                    Catalog(connection).attach_to_series(series_id, work_id, position)
                    plan = plan_series_reassignment(connection, work_id)
                    self._audit(
                        connection,
                        principal,
                        "series.attach",
                        f"{work_id};{series_id}",
                        always=True,
                    )
        except SeriesAlreadyAttachedError:
            raise CodedRefusal("series.already_attached", work_id) from None
        except exc.IntegrityError:
            raise UnknownSubjectError("no such Work or Series") from None
        return {
            "work_id": work_id, "series_id": series_id, "position": position,
            "reassignment": _series_report(plan, False),
        }  # fmt: skip
