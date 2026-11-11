"""`edition merge` (EF-BIB-049, EF-BIB-054 (4), EF-BIB-055): the plan of a merge of two Editions
of a Work, and its application for the fingerprint of that plan.

A reserved action (`edition.merge`): the host CLI or a fresh admin session, never a key. The plan
reads the tombstones to say which Units a merge would bring back, so it is reserved too, and it
writes nothing. The merge acts only for the fingerprint of a plan the admin saw, recomputed under
the write lock; its audit entry is written in the same transaction as the rows it moved. A hit of
a tombstone is told on the bus once the lock is let go (the bus writes in its own transaction).

`proposals` is what a merge of contributors proposes (EF-BIB-049): the plans of the Editions that
would then share a key. It applies nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.public import (
    EditionMergePlan,
    EditionMergeRefusedError,
    StaleMergePlanError,
    apply_edition_merge,
    plan_after_contributor_merge,
    plan_edition_merge,
)
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity
from shelfhold.ops.errors import CodedRefusal, StalePreviewError, UnknownSubjectError
from shelfhold.ops.tombstone_screens import CatalogScreens

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["EditionMergeOps"]


def _report(plan: EditionMergePlan, applied: bool) -> dict[str, Any]:
    return {
        "keep_id": plan.keep_id, "drop_id": plan.drop_id, "work_id": plan.work_id,
        "units": list(plan.units), "volumes": list(plan.volumes),
        "bindings": list(plan.bindings), "blockers": list(plan.blockers),
        "tombstoned_units": len(plan.tombstoned_units), "tombstoned_ids": plan.tombstoned_ids,
        "keep_age": plan.keep_age, "drop_age": plan.drop_age, "merged_age": plan.merged_age,
        "unrated_after": plan.unrated_after, "mergeable": plan.mergeable,
        "fingerprint": plan.fingerprint, "applied": applied,
    }  # fmt: skip


class EditionMergeOps:
    def __init__(self, comp: Composition, screens: CatalogScreens | None = None) -> None:
        self._comp = comp
        self._screens = screens or CatalogScreens(comp)

    def _authorize(self, principal: Principal) -> None:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["edition.merge"], now=self._comp.clock.now(),
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the socket decided first; this one is the last gate
            raise CodedRefusal("forbidden", type(verdict).__name__)

    def merge(
        self, keep_id: str, drop_id: str, *, confirm: str | None, principal: Principal
    ) -> dict[str, Any]:
        """The plan to merge `drop_id` into `keep_id`; applied too when `confirm` is the
        fingerprint of that plan."""
        self._authorize(principal)
        screen = self._screens.open()
        engine = self._comp.engine
        try:
            if confirm is None:
                with engine.connect() as connection:
                    shown = plan_edition_merge(connection, keep_id, drop_id, screen=screen)
                screen.announce()
                return _report(shown, False)
            with engine.connect() as connection:
                connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
                with connection.begin():
                    done = apply_edition_merge(
                        connection, keep_id, drop_id, confirm=confirm, screen=screen,
                        audit=self._audit(principal),
                    )  # fmt: skip
            return _report(done.plan, True)
        except LookupError as error:
            raise UnknownSubjectError(str(error)) from None
        except ValueError as error:
            raise CodedRefusal("edition.merge_refused", str(error)) from None
        except StaleMergePlanError as error:
            raise StalePreviewError(str(error)) from None
        except EditionMergeRefusedError as error:
            screen.announce()
            raise CodedRefusal("edition.merge_refused", ", ".join(error.plan.reasons)) from None

    def proposals(
        self, keep_contributor_id: str, drop_contributor_id: str, *, principal: Principal
    ) -> list[dict[str, Any]]:
        """The merge plans a merge of these two contributors would call for. Nothing is applied."""
        self._authorize(principal)
        screen = self._screens.open()
        with self._comp.engine.connect() as connection:
            plans = plan_after_contributor_merge(
                connection, keep_contributor_id, drop_contributor_id, screen=screen
            )
        screen.announce()
        return [_report(p, False) for p in plans]

    @staticmethod
    def _audit(principal: Principal) -> Any:
        def write(connection: Connection, plan: EditionMergePlan) -> None:
            connection.execute(
                sa.insert(identity.audit).values(
                    id=new_ulid(), actor_id=principal.actor_id, action="edition.merge",
                    target=f"{plan.keep_id};{plan.drop_id};fp:{plan.fingerprint};"
                    f"units:{len(plan.units)}",
                    session_id=principal.session_id, auth_time=principal.auth_time,
                )
            )  # fmt: skip

        return write
