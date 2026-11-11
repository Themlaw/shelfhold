"""The fallback of a followed Edition to its next ranked binding (EF-SRC-037, EF-INT-006).

A binding is dead when its upstream source is not loaded for three cycles in a row (EF-SRC-026),
when its content errors in a row reach the threshold of EF-BIB-042, or when its series left the
upstream library (EF-SRC-027). It then goes inactive with a `disabled_reason`, its open requests
are cancelled (the Unit is asked again of the next rank by the next election) and
`source.fallback` is told: one event per transition, and never a switch without it. A binding with
no other active binding in its own Edition and Work is left active: there is nowhere to fall back
to, and what it did before (ask nothing until its source is back) still holds.

The return to the original rank (a setting) only undoes a fallback for an unloaded source: what
the admin disabled (`manual`), the content errors and what left upstream are never switched back
on from here.
"""

from collections.abc import Callable, Collection
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import acquisition
from shelfhold.library.public import CANDIDATE_AFTER

__all__ = ["FALLBACK_REASONS", "NOT_LOADED_CYCLES", "Emit", "Fallback"]

log = structlog.get_logger(__name__)

Emit = Callable[[Connection, str, dict[str, object]], None]

NOT_LOADED_CYCLES: Final = 3  # consecutive cycles an upstream source stays unloaded
FALLBACK_REASONS: Final = ("not_loaded", "content_failures", "removed_upstream")
_OPEN = ("requested", "downloaded")


class Fallback:
    def __init__(
        self,
        emit: Emit,
        *,
        not_loaded_after: int = NOT_LOADED_CYCLES,
        content_after: int = CANDIDATE_AFTER,
        return_to_origin: bool = True,
    ) -> None:
        self._emit = emit
        self._not_loaded_after = not_loaded_after
        self._content_after = content_after
        self._return = return_to_origin

    @staticmethod
    def target(
        connection: Connection, binding_id: str, usable: Collection[str] | None = None
    ) -> str | None:
        """The binding that takes over: another active one of the same Edition (and so of the
        same Work), the next rank first; among `usable` when given (what the cycle may ask)."""
        b = acquisition.source_binding
        me = connection.execute(
            sa.select(b.c.edition_id, b.c.work_id, b.c.priority).where(b.c.id == binding_id)
        ).one_or_none()
        if me is None:
            return None
        others = connection.execute(
            sa.select(b.c.id, b.c.priority)
            .where(
                b.c.edition_id == me.edition_id, b.c.work_id == me.work_id,
                b.c.active, b.c.id != binding_id,
            )
            .order_by(b.c.priority)
        ).all()  # fmt: skip
        if usable is not None:
            others = [o for o in others if o.id in usable]
        after = [o for o in others if o.priority > me.priority]
        chosen = after[0] if after else (others[0] if others else None)
        return None if chosen is None else str(chosen.id)

    def dead_reason(
        self, connection: Connection, binding_id: str, *, not_loaded_cycles: int
    ) -> str | None:
        """Why this active binding counts as dead, if it does."""
        if not_loaded_cycles >= self._not_loaded_after:
            return "not_loaded"
        failure = acquisition.source_failure
        worst = connection.execute(
            sa.select(sa.func.max(failure.c.consecutive_content_errors)).where(
                failure.c.source_binding_id == binding_id, failure.c.failure_class == "content"
            )
        ).scalar()
        if worst is not None and worst >= self._content_after:
            return "content_failures"
        return None

    def fall_back(
        self,
        connection: Connection,
        binding_id: str,
        reason: str,
        usable: Collection[str] | None = None,
    ) -> bool:
        """True when the binding went inactive for `reason` in this call (and `source.fallback`
        was told). False when there is no other binding to take over or it already had."""
        if reason not in FALLBACK_REASONS:
            raise ValueError(f"not a fallback reason: {reason!r}")
        successor = self.target(connection, binding_id, usable)
        if successor is None:
            return False
        b = acquisition.source_binding
        row = connection.execute(
            sa.select(b.c.edition_id, b.c.work_id).where(b.c.id == binding_id)
        ).one()
        changed = connection.execute(
            sa.update(b)
            .where(b.c.id == binding_id, b.c.active)
            .values(active=False, disabled_reason=reason)
        ).rowcount
        if not changed:
            return False  # a concurrent cycle got there first: it told it
        self._postpone(connection, binding_id)
        self._emit(
            connection, "source.fallback",
            {
                "edition_id": row.edition_id, "work_id": row.work_id,
                "from_binding_id": binding_id, "to_binding_id": successor, "reason": reason,
            },
        )  # fmt: skip
        log.warning("follow.fallback", binding_id=binding_id, to=successor, reason=reason)
        return True

    def announce_removed(
        self, connection: Connection, binding_id: str, usable: Collection[str] | None = None
    ) -> bool:
        """The binding left upstream (it is already inactive): its open requests are postponed and
        the fallback is told, if another binding can take over."""
        b = acquisition.source_binding
        row = connection.execute(
            sa.select(b.c.edition_id, b.c.work_id).where(b.c.id == binding_id)
        ).one()
        self._postpone(connection, binding_id)
        successor = self.target(connection, binding_id, usable)  # the others, which are active
        if successor is None:
            return False
        self._emit(
            connection, "source.fallback",
            {
                "edition_id": row.edition_id, "work_id": row.work_id,
                "from_binding_id": binding_id, "to_binding_id": successor,
                "reason": "removed_upstream",
            },
        )  # fmt: skip
        log.warning(
            "follow.fallback", binding_id=binding_id, to=successor, reason="removed_upstream"
        )
        return True

    @staticmethod
    def _postpone(connection: Connection, binding_id: str) -> None:
        """Its open requests stop waiting on a source that no longer answers: the next election
        asks the next rank for those Units (the unique open ticket per Unit is free again)."""
        r = acquisition.download_request
        connection.execute(
            sa.update(r)
            .where(
                r.c.source_binding_id == binding_id, r.c.state.in_(_OPEN), r.c.unit_id.is_not(None)
            )
            .values(state="cancelled")
        )

    def restore(self, connection: Connection, binding_id: str) -> bool:
        """The source is loaded again: back to the original rank, if the setting allows. Only an
        unloaded source comes back by itself: a binding that fell back for its content errors has
        no probe that says it is healthy (the admin re-enables it), and what the admin disabled
        or what left upstream is never switched back on from here."""
        if not self._return:
            return False
        b = acquisition.source_binding
        me = connection.execute(
            sa.select(b.c.edition_id, b.c.priority, b.c.disabled_reason).where(
                b.c.id == binding_id, sa.not_(b.c.active)
            )
        ).one_or_none()
        if me is None or me.disabled_reason != "not_loaded":
            return False
        taken = connection.execute(
            sa.select(b.c.id).where(
                b.c.edition_id == me.edition_id, b.c.priority == me.priority, b.c.active
            )
        ).first()
        if taken is not None:
            return False
        changed = connection.execute(
            sa.update(b)
            .where(b.c.id == binding_id, sa.not_(b.c.active))
            .values(active=True, disabled_reason=None)
        ).rowcount
        if changed:
            log.info("follow.fallback_returned", binding_id=binding_id)
        return bool(changed)
