"""`rate`: the only door that sets an age (EF-BIB-036), and the questions that follow from it.

A reserved action (`rate`): the host CLI or an admin session holding the right to classify, never
a key. The caller owns the transaction; the audit entry is written in it, with the rows. There is
no command yet (L11): this is the service it will call.

Rating a Work stamps `rated_at` on the Editions it contained; an Edition that arrives later keeps
a null `rated_at` and is unrated until an admin rates (EF-BIB-055). Raising an age returns a plan
of the Editions to withdraw from shelves that no longer fit; nothing is withdrawn by itself.
"""

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.domain.age import (
    Admission,
    EditionAge,
    admission,
    ceiling_of,
    effective_age,
)
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    AuditEntry,
    Principal,
    audit_insert,
    authorize_reserved,
)
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.sdk.age import AGE_TIERS, AgeRating, label_key, to_columns

__all__ = ["RatingRefusedError", "RatingResult", "RatingService", "Withdrawal"]


class RatingRefusedError(Exception):
    """The caller may not rate (the verdict of `authorize_reserved` is the message)."""


@dataclass(frozen=True)
class Withdrawal:
    edition_id: str
    shelf_id: str
    reason: Admission


@dataclass(frozen=True)
class RatingResult:
    target: str  # `work:<id>` or `edition:<id>`
    rating: AgeRating
    label: str  # i18n key
    effective: AgeRating | None  # of the Work, once rated
    withdrawals: tuple[Withdrawal, ...]


def _age(age_rating: int | None, age_x: bool) -> AgeRating | None:
    if age_x:
        return AgeRating(18, x=True)
    return None if age_rating is None else AgeRating(int(age_rating))


class RatingService:
    def __init__(self, *, fresh_auth_s: int = 900) -> None:
        self._fresh_auth_s = fresh_auth_s

    # -- writes ---------------------------------------------------------------------------------
    def rate(
        self, connection: Connection, work_id: str, rating: AgeRating, *,
        principal: Principal, now: datetime,
    ) -> RatingResult:  # fmt: skip
        """Fix the age of a Work; the Editions it already holds count as rated with it."""
        self._allow(principal, now)
        _check_tier(rating)
        age, x = to_columns(rating)
        work, edition = catalog.work, catalog.edition
        found = connection.execute(
            sa.update(work)
            .where(work.c.id == work_id)
            .values(age_rating=age, age_x=x, rated_at=now)
        )
        if found.rowcount != 1:
            raise LookupError(f"no such Work: {work_id}")
        connection.execute(
            sa.update(edition)
            .where(edition.c.work_id == work_id, edition.c.rated_at.is_(None))
            .values(rated_at=now)
        )
        return self._done(connection, f"work:{work_id}", work_id, rating, principal, now)

    def rate_edition(
        self, connection: Connection, edition_id: str, rating: AgeRating, *,
        principal: Principal, now: datetime,
    ) -> RatingResult:  # fmt: skip
        """Fix the age of one Edition (an adaptation for children of an adult Work, say)."""
        self._allow(principal, now)
        _check_tier(rating)
        age, x = to_columns(rating)
        edition = catalog.edition
        row = connection.execute(
            sa.update(edition)
            .where(edition.c.id == edition_id)
            .values(age_rating=age, age_x=x, rated_at=now)
            .returning(edition.c.work_id)
        ).first()
        if row is None:
            raise LookupError(f"no such Edition: {edition_id}")
        return self._done(connection, f"edition:{edition_id}", row.work_id, rating, principal, now)

    # -- reads ----------------------------------------------------------------------------------
    def effective_age_of(self, connection: Connection, work_id: str) -> AgeRating | None:
        work, edition = catalog.work, catalog.edition
        own = connection.execute(
            sa.select(work.c.age_rating, work.c.age_x).where(work.c.id == work_id)
        ).one()
        rows = connection.execute(
            sa.select(edition.c.age_rating, edition.c.age_x).where(edition.c.work_id == work_id)
        )
        return effective_age(_age(own.age_rating, own.age_x), (_age(r[0], r[1]) for r in rows))

    def admission_of(self, connection: Connection, edition_id: str, *, shelf_id: str) -> Admission:
        ceiling = connection.execute(
            sa.select(export.shelf.c.max_age_rating).where(export.shelf.c.id == shelf_id)
        ).scalar_one()
        return admission(self._facts(connection, edition_id), ceiling_of(ceiling))

    # -- inside ---------------------------------------------------------------------------------
    def _allow(self, principal: Principal, now: datetime) -> None:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["rate"], now=now, fresh_auth_s=self._fresh_auth_s
        )
        if not isinstance(verdict, Allowed):
            raise RatingRefusedError(type(verdict).__name__)

    def _done(
        self, connection: Connection, target: str, work_id: str, rating: AgeRating,
        principal: Principal, now: datetime,
    ) -> RatingResult:  # fmt: skip
        connection.execute(
            audit_insert(
                AuditEntry(
                    actor_id=principal.actor_id,
                    action="rate",
                    at=now,
                    target=target,
                    session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )
        )
        return RatingResult(
            target, rating, label_key(rating), self.effective_age_of(connection, work_id),
            self._withdrawals(connection, work_id),
        )  # fmt: skip

    def _withdrawals(self, connection: Connection, work_id: str) -> tuple[Withdrawal, ...]:
        edition, shelf = catalog.edition, export.shelf
        filed = connection.execute(
            sa.select(edition.c.id, edition.c.shelf_id, shelf.c.max_age_rating)
            .join(shelf, shelf.c.id == edition.c.shelf_id)
            .where(edition.c.work_id == work_id)
            .order_by(edition.c.id)
        ).all()
        plan = []
        for edition_id, shelf_id, ceiling in filed:
            verdict = admission(self._facts(connection, edition_id), ceiling_of(ceiling))
            if verdict is not Admission.ALLOWED:
                plan.append(Withdrawal(edition_id, shelf_id, verdict))
        return tuple(plan)

    def _facts(self, connection: Connection, edition_id: str) -> EditionAge:
        edition, suggestion = catalog.edition, catalog.age_suggestion
        row = connection.execute(
            sa.select(
                edition.c.work_id, edition.c.age_rating, edition.c.age_x, edition.c.rated_at
            ).where(edition.c.id == edition_id)
        ).one()
        seen = connection.execute(
            sa.select(suggestion.c.value, suggestion.c.x).where(
                sa.or_(
                    sa.and_(
                        suggestion.c.entity_type == "edition", suggestion.c.entity_id == edition_id
                    ),
                    sa.and_(
                        suggestion.c.entity_type == "work", suggestion.c.entity_id == row.work_id
                    ),
                ),
                suggestion.c.state != "rejected",
            )
        )
        return EditionAge(
            explicit=_age(row.age_rating, row.age_x),
            rated=row.rated_at is not None,
            work=self.effective_age_of(connection, row.work_id),
            suggested=tuple(AgeRating(18, True) if x else AgeRating(int(v)) for v, x in seen),
        )


def _check_tier(rating: AgeRating) -> None:
    if rating.min_age not in AGE_TIERS:
        raise ValueError(f"age {rating.min_age} is not on the scale of tiers {AGE_TIERS}")
