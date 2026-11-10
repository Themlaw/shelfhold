"""`content reneutralize`: the explicit migration after a security update of the neutralizer
(EF-BIB-051, ENF-OPS-021 for the manner: plan, then confirmation, then application).

A new release of `xhtml-neutralizer` never rewrites anything by itself: each derivative stays under
the release that made it (`derived_by`) until an admin migrates. The **plan** reads and writes
nothing: for every EPUB whose derivative was made by another release it says where the
regeneration would restart from (the original in `/store`, else the derivative in `/library`; the
generation replaced goes to `derived_chain`) or why it cannot (`blocked`). The **application** runs
only for the fingerprint of a plan the admin saw, recomputed at that moment, and replaces each
derivative by the primitive of an atomic replacement, after verifying the new file: nothing is
deleted first, and a content that fails keeps its old file and rows.
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.infrastructure.content_trust import DERIVED_BY, neutralize_epub
from shelfhold.infrastructure.schema import storage

log = structlog.get_logger(__name__)


Neutralize = Callable[..., Any]  # `(source, out, **kw) -> Derivative`, `neutralize_epub`'s shape


class EpubRegenerator(Protocol):
    """What the migration needs of the store of EPUB derivatives (`UntrustedEpubStore`)."""

    def source_of(self, unit_id: str) -> tuple[Literal["original", "derived", "blocked"], str]: ...

    def regenerate(
        self, unit_id: str, *, neutralize: Neutralize | None = ..., target: str = ...
    ) -> Any: ...


class StalePlanError(RuntimeError):
    """The fingerprint is not the one of the plan as it stands now: look again."""


@dataclass(frozen=True)
class PlanItem:
    unit_id: str
    content_id: str
    from_by: str
    to_by: str
    source: Literal["original", "derived", "blocked"]
    reason: str = ""


@dataclass(frozen=True)
class ReneutralizePlan:
    items: tuple[PlanItem, ...]
    fingerprint: str


@dataclass(frozen=True)
class ReneutralizeReport:
    regenerated: int
    blocked: int
    failed: int
    items: tuple[PlanItem, ...] = ()


def _fingerprint(items: Sequence[PlanItem]) -> str:
    body = json.dumps(
        [[i.unit_id, i.content_id, i.from_by, i.to_by, i.source, i.reason] for i in items]
    )
    return hashlib.sha256(body.encode()).hexdigest()


class Reneutralizer:
    def __init__(
        self,
        store: EpubRegenerator,
        *,
        engine: Engine,
        neutralize: Neutralize = neutralize_epub,
        target: str = DERIVED_BY,
    ) -> None:
        self._store = store
        self._engine = engine
        self._neutralize = neutralize
        self._target = target

    def plan(self) -> ReneutralizePlan:
        content = storage.content
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(content.c.id, content.c.unit_id, content.c.derived_by)
                .where(
                    content.c.format == "epub",
                    content.c.derived_hash.is_not(None),
                    content.c.derived_by != self._target,
                    content.c.unit_id.is_not(None),
                )
                .order_by(content.c.id)
            ).all()
        items: list[PlanItem] = []
        for row in rows:
            source, why = self._store.source_of(row.unit_id)
            items.append(
                PlanItem(
                    row.unit_id,
                    row.id,
                    row.derived_by,
                    self._target,
                    source,
                    why if source == "blocked" else "",
                )
            )
        plan = tuple(items)
        return ReneutralizePlan(plan, _fingerprint(plan))

    def apply(self, fingerprint: str) -> ReneutralizeReport:
        plan = self.plan()
        if plan.fingerprint != fingerprint:
            raise StalePlanError("the plan changed since it was shown")
        done = blocked = failed = 0
        for item in plan.items:
            if item.source == "blocked":
                blocked += 1
                continue
            try:
                self._store.regenerate(
                    item.unit_id, neutralize=self._neutralize, target=self._target
                )
            except Exception as error:  # the old file and rows are untouched: say, go on
                failed += 1
                log.warning("content.reneutralize_failed", content_id=item.content_id,
                            error=type(error).__name__)  # fmt: skip
                continue
            done += 1
        return ReneutralizeReport(done, blocked, failed, plan.items)


def as_dict(plan: ReneutralizePlan) -> dict[str, Any]:
    return {
        "fingerprint": plan.fingerprint,
        "items": [
            {"unit": i.unit_id, "from": i.from_by, "to": i.to_by, "source": i.source,
             "reason": i.reason}
            for i in plan.items
        ],
    }  # fmt: skip
