"""`rate` and "classer tout en lot" (EF-BIB-036, EF-INT-014, EF-INT-023, EF-EXP-048).

`rate` is the way out of both "À classer" states (`rating_pending`, `follow_suspended`). It is a
reserved action: the host CLI or a fresh admin session holding the right to classify, never a key.
Everything goes through `RatingService`, which checks that again (the route and the socket decide
first, this is the last gate), writes the audit entry in the transaction of the rows and returns
the plan of the files a raised age no longer lets stay on a shelf below 18+. The plan is told, not
applied: taking a file out of the library of the children is `shelf withdraw`.

The batch rates every Work that waits at one age. It acts only for the token of the list the admin
read (the count and a digest of the Works), recomputed under the write lock, and it writes one
audit entry per Work, all in one transaction: all of them or none.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any

from shelfhold.catalog.public import (
    RatingRefusedError,
    RatingResult,
    RatingService,
    parse_age,
)
from shelfhold.export.application.to_rate import ToRate, batch_token, to_rate
from shelfhold.identity.public import Principal
from shelfhold.ops.errors import (
    CodedRefusal,
    InvalidChoiceError,
    StalePreviewError,
    UnknownSubjectError,
)
from shelfhold.sdk.age import AGE_TIERS, AgeRating

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from shelfhold.infrastructure.clock import Clock

__all__ = ["RatingOps"]


def _item(i: ToRate) -> dict[str, str]:
    return {"work_id": i.work_id, "title": i.title, "state": i.state, "reason": i.reason}


def _result(r: RatingResult) -> dict[str, Any]:
    return {
        "target": r.target,
        "label": r.label,
        "effective": r.effective.min_age if r.effective else None,
        "effective_x": bool(r.effective and r.effective.x),
        "withdrawals": [
            {"edition_id": w.edition_id, "shelf_id": w.shelf_id, "reason": w.reason.value}
            for w in r.withdrawals
        ],
    }


def _age(text: str) -> AgeRating:
    try:
        rating = parse_age(text)
    except ValueError as error:
        raise InvalidChoiceError(str(error)) from None
    if rating.min_age not in AGE_TIERS:
        raise InvalidChoiceError(f"choose one of: {', '.join(f'{t}+' for t in AGE_TIERS)}, X")
    return rating


class RatingOps:
    def __init__(self, engine: Engine, clock: Clock, *, fresh_auth_s: int = 900) -> None:
        self._engine = engine
        self._clock = clock
        self._service = RatingService(fresh_auth_s=fresh_auth_s)

    def rate(
        self, age: str, *, work_id: str | None, edition_id: str | None, principal: Principal
    ) -> dict[str, Any]:
        """Fix the age of a Work, or of one Edition of it. A raised age that no longer fits a
        shelf below 18+ comes back as the plan of the files to withdraw; nothing moves."""
        if (work_id is None) == (edition_id is None):
            raise InvalidChoiceError("name a Work or an Edition, not both and not neither")
        rating = _age(age)
        service = self._service
        try:
            with self._engine.begin() as connection:
                now = self._clock.now()
                if work_id is not None:
                    done = service.rate(connection, work_id, rating, principal=principal, now=now)
                elif edition_id is not None:
                    done = service.rate_edition(
                        connection, edition_id, rating, principal=principal, now=now
                    )
                else:  # unreachable: one of the two is named
                    raise InvalidChoiceError("name a Work or an Edition")
        except RatingRefusedError as error:
            raise CodedRefusal("forbidden", str(error)) from None
        except LookupError as error:
            raise UnknownSubjectError(str(error)) from None
        return _result(done)

    def pending(self, age: str) -> dict[str, Any]:
        """The Works that wait in "À classer" and the token that rates them all at `age` (read
        only: nothing is rated)."""
        text = _age_text(_age(age))
        with self._engine.connect() as connection:
            items = to_rate(connection)
        return {"age": text, "items": [_item(i) for i in items], "token": batch_token(items, text)}

    def rate_batch(self, age: str, *, confirm: str, principal: Principal) -> dict[str, Any]:
        """Rate every Work that waits, for the token that was read; one audit entry per Work."""
        rating = _age(age)
        text = _age_text(rating)
        service = self._service
        results: list[dict[str, Any]] = []
        try:
            with self._engine.connect() as connection:
                connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
                with connection.begin():
                    items = to_rate(connection)  # again, under the write lock
                    if not hmac.compare_digest(
                        confirm.strip().encode(), batch_token(items, text).encode()
                    ):
                        raise StalePreviewError("the list changed since it was read: look again")
                    now = self._clock.now()
                    for item in items:
                        done = service.rate(
                            connection, item.work_id, rating, principal=principal, now=now
                        )
                        results.append({"work_id": item.work_id, **_result(done)})
        except RatingRefusedError as error:
            raise CodedRefusal("forbidden", str(error)) from None
        return {"age": text, "rated": results}


def _age_text(rating: AgeRating) -> str:
    return "X" if rating.x else f"{rating.min_age}+"
