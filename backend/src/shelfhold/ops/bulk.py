"""Bulk rating of the Works that wait in "À classer", through the queue (M4 L28, EF-BIB-023).

Two steps and one job, on an explicit selection:

1. `rate_preview` takes either the identifiers of the Works chosen, or a filter (a state, a text);
   it keeps only those that wait *now* and **freezes** them into an order (`library.bulk`). Nothing
   is rated. What it answers is the order's id, the Works that would be rated (their titles), how
   many ids were skipped (not named: rated, absent or someone else's all look alike) and the token
   that applies it. A filter is frozen into the identifiers it matched: a Work that matches it
   later is not in the order, so a filter cannot widen.
2. `rate_queue` names the order and its token, nothing else, and queues **one** job
   `library.bulk_rate:<order id>`. The job holds identifiers only (no age, no token).

The job reads the order again and, under the write lock, recomputes which of the chosen Works still
wait: if that is not what the preview froze, the order is `stale` and nothing is rated (all or
none); a Work that is not in the selection is never touched, whatever the list became. It checks
that the order's owner still holds the right to classify. One audit line per Work, naming the order.

Both doors are the reserved action `rate` (a fresh admin session holding the right to classify,
never a key, a member or a child): the same gate is asked again here, the last one. Bringing the
job back (`retry`, `replay`) is that action too (`ops.job_actions`).
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from shelfhold.catalog.public import RatingRefusedError, RatingService
from shelfhold.export.application.to_rate import ToRate, rateable, to_rate
from shelfhold.identity.public import Credential, Principal, holds_rate_right
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import identity as identity_schema
from shelfhold.jobs.public import JobContext, JobFailedError
from shelfhold.library.public import bulk
from shelfhold.ops.errors import (
    CodedRefusal,
    ExitUnavailableError,
    InvalidChoiceError,
    NotWaitingError,
    StalePreviewError,
    UnknownSubjectError,
)
from shelfhold.ops.rating import age_text, parse_age_choice

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from shelfhold.infrastructure.clock import Clock

__all__ = ["BULK_RATE_JOB_KIND", "BulkOps", "register_bulk_jobs"]

BULK_RATE_JOB_KIND: Final = "library.bulk_rate"
RATE: Final = "rate"
FILTER_STATES: Final = ("rating_pending", "follow_suspended")
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_ABSENT = "no such order"  # one answer for every reason an order cannot be applied by this caller


class BulkOps:
    def __init__(
        self, engine: Engine, clock: Clock, *, fresh_auth_s: int = 900,
        pools: Callable[[], Any] | None = None,
    ) -> None:  # fmt: skip
        self._engine = engine
        self._clock = clock
        self._service = RatingService(fresh_auth_s=fresh_auth_s)
        self._pools_of = pools

    def attach_pools(self, pools: Any) -> None:
        if self._pools_of is None:
            self._pools_of = lambda: pools

    # -- the preview ------------------------------------------------------------------------

    def rate_preview(
        self, *, age: str, principal: Principal, work_ids: list[str] | None = None,
        filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """Freeze the selection into an order; nothing is rated, nothing is queued."""
        self._gate(principal)
        text = age_text(parse_age_choice(age))
        if (work_ids is None) == (filter is None):
            raise InvalidChoiceError("name the Works chosen, or a filter, not both and not neither")
        with self._engine.connect() as connection:
            waiting = rateable(to_rate(connection))
        if work_ids is not None:
            wanted = _identifiers(work_ids)
            matched = [i for i in waiting if i.work_id in wanted]
            skipped = len(wanted) - len(matched)
        else:
            matched = _filtered(waiting, filter or {})
            skipped = 0
        if not matched:
            raise InvalidChoiceError("none of the Works chosen waits in the list")
        if len(matched) > bulk.MAX_SELECTION:
            raise InvalidChoiceError(f"at most {bulk.MAX_SELECTION} Works in one order: narrow it")
        now = self._clock.now()
        with write_transaction(self._engine) as connection:
            order = bulk.freeze(
                connection,
                kind=RATE,
                actor_id=principal.user_id,
                params={"age": text},
                work_ids=[i.work_id for i in matched],
                now=now,
            )
        return {
            "order_id": order.id, "age": text, "token": order.token, "skipped": skipped,
            "expires_at": (now + bulk.ORDER_TTL).isoformat(),
            "items": [
                {"work_id": i.work_id, "title": i.title, "state": i.state} for i in matched
            ],
        }  # fmt: skip

    # -- the order --------------------------------------------------------------------------

    def rate_queue(self, *, order_id: str, confirm: str, principal: Principal) -> str:
        """Queue the one job of the order; returns its id. The order is read for this user only:
        another profile's order, a forged id and an expired preview are the same absence."""
        self._gate(principal)
        now = self._clock.now()
        with self._engine.connect() as connection:
            order = (
                bulk.load(connection, order_id, actor_id=principal.user_id, now=now)
                if _ULID.match(order_id)
                else None
            )
        if order is None or order.kind != RATE:
            raise UnknownSubjectError(_ABSENT)
        if order.state != "previewed":
            raise NotWaitingError("this order was already applied")
        if not hmac.compare_digest(confirm.strip().encode(), order.token.encode()):
            raise StalePreviewError("the order is not the one that was previewed: look again")
        pools = self._pools()  # a refusal comes first: no state is touched for a queue that is off
        with write_transaction(self._engine) as connection:
            if not bulk.claim(connection, order.id):  # another apply took it first
                raise NotWaitingError("this order was already applied")
        try:
            job_id: str = pools.submit(
                BULK_RATE_JOB_KIND,
                dedupe_key=f"{BULK_RATE_JOB_KIND}:{order.id}",
                priority_class="request",
            ).job_id
        except BaseException:
            with write_transaction(self._engine) as connection:
                bulk.release(connection, order.id)
            raise
        with write_transaction(self._engine) as connection:
            bulk.attach(connection, order.id, job_id=job_id)
        return job_id

    def _pools(self) -> Any:
        pools = self._pools_of() if self._pools_of else None
        if pools is None:
            raise ExitUnavailableError("the job queue is not running")
        return pools

    def _gate(self, principal: Principal) -> None:
        """The reserved action `rate`, for a session of an admin: never a key, a token, the host
        or a believed proxy (they have no profile to own an order), a member or a child."""
        if (
            principal.credential is not Credential.SESSION
            or principal.via_forward
            or principal.role != "admin"
        ):
            raise CodedRefusal("forbidden", "not_allowed")
        try:
            self._service.authorize(principal, self._clock.now())
        except RatingRefusedError as error:
            raise CodedRefusal("forbidden", str(error)) from None

    # -- the job ----------------------------------------------------------------------------

    def run_rate_job(self, context: JobContext) -> None:
        """The handler of `library.bulk_rate`: what the order says, once, or nothing."""
        _kind, _sep, order_id = context.dedupe_key.partition(":")
        if _kind != BULK_RATE_JOB_KIND or not _ULID.match(order_id):
            raise JobFailedError("job.failed")
        outcome, summary = self._apply(order_id)
        if outcome == "applied_before":
            return  # a run that came back after the order was applied: nothing left to do
        context.record(**summary)
        if outcome != "applied":
            raise JobFailedError(f"bulk.{outcome}")

    def _apply(self, order_id: str) -> tuple[str, dict[str, int]]:
        now = self._clock.now()
        results: dict[str, int] = {"rated": 0, "withdrawals": 0}
        with self._engine.connect() as connection:
            connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
            with connection.begin():
                order = bulk.load_for_job(connection, order_id)
                if order is None or order.kind != RATE:
                    return "unknown", {}
                if order.state == "applied":
                    return "applied_before", {}
                if order.state != "queued":  # `stale` or `refused` already: it does not come back
                    return order.state if order.state in {"stale", "refused"} else "unknown", {}
                if not self._owner_may_rate(connection, order.actor_id):
                    bulk.close(connection, order.id, "refused", now=now)
                    return "refused", {}
                chosen = set(order.work_ids)
                waiting = [i for i in rateable(to_rate(connection)) if i.work_id in chosen]
                digest = bulk.digest_of(RATE, order.params, (i.work_id for i in waiting))
                if not hmac.compare_digest(digest.encode(), order.digest.encode()):
                    bulk.close(connection, order.id, "stale", now=now)  # all or none
                    return "stale", {}
                rating = parse_age_choice(str(order.params["age"]))
                for item in waiting:
                    done = self._service.rate_ordered(
                        connection,
                        item.work_id,
                        rating,
                        order_id=order.id,
                        decided_by=order.actor_id,
                        now=now,
                    )
                    results["rated"] += 1
                    results["withdrawals"] += len(done.withdrawals)
                bulk.close(connection, order.id, "applied", now=now, summary=dict(results))
        return "applied", results

    @staticmethod
    def _owner_may_rate(connection: sa.Connection, user_id: str) -> bool:
        user = identity_schema.user
        row = connection.execute(
            sa.select(user.c.role, user.c.can_rate).where(user.c.id == user_id)
        ).first()
        return bool(row and row.role == "admin" and holds_rate_right(row.role, bool(row.can_rate)))


def register_bulk_jobs(pools: Any, ops: BulkOps) -> None:
    """The kind of job of M4 L28. A write job: a pause of the queue parks it."""
    ops.attach_pools(pools)
    pools.register(BULK_RATE_JOB_KIND, pool="io", handler=ops.run_rate_job)


def _identifiers(work_ids: list[str]) -> set[str]:
    if not work_ids:
        raise InvalidChoiceError("name at least one Work")
    wanted = set(work_ids)
    if len(wanted) > bulk.MAX_SELECTION or any(not _ULID.match(w) for w in wanted):
        raise InvalidChoiceError("a list of at most 1000 Work identifiers")
    return wanted


def _filtered(waiting: list[ToRate], flt: dict[str, Any]) -> list[ToRate]:
    state = flt.get("state")
    text = str(flt.get("text") or "").strip().casefold()
    if state is not None and state not in FILTER_STATES:
        raise InvalidChoiceError("choose a state of the list")
    return [
        i for i in waiting
        if (state is None or i.state == state) and text in i.title.casefold()
    ]  # fmt: skip
