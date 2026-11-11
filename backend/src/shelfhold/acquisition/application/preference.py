"""Election of the Variant to ask for and to export, by team (EF-BIB-028).

The explicit priority list of the Edition wins. Without it the default rule of
`domain.team_policy` applies, on figures read here: files on disk per team, what each team
offers, when it last released. Departing from the team that holds the majority of the files is a
*fallback*, and every fallback is written to the log with its reason: a translation never changes
in the middle of a series without a trace.

A Unit that already has a ticket (open or fetched) keeps the Variant it was asked for: no upgrade
before EF-EXP-016.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection

from shelfhold.acquisition.application.filtering import refusals, set_filtered_reason
from shelfhold.acquisition.domain.selection import Candidate, select
from shelfhold.acquisition.domain.team_policy import (
    DEFAULT_INACTIVE_MONTHS,
    TeamChoice,
    TeamKey,
    TeamStats,
    choose_team,
)
from shelfhold.catalog.public import Catalog, VariantFacts
from shelfhold.infrastructure.schema import acquisition, catalog

log = structlog.get_logger(__name__)

_ASKED = ("requested", "downloaded", "fetched")


@dataclass(frozen=True)
class Election:
    variant_id: str
    binding_id: str
    choice: TeamChoice


def record_release(connection: Connection, team_id: str | None, at: datetime) -> None:
    """The team released something at `at`: the clock of "inactive for more than N months"."""
    if team_id is None:
        return
    team = catalog.team
    connection.execute(
        sa.update(team)
        .where(
            team.c.id == team_id,
            sa.or_(team.c.last_release_at.is_(None), team.c.last_release_at < at),
        )
        .values(last_release_at=at)
    )


def _reason_shown(candidates: list[Candidate]) -> str | None:
    """Why the Unit is left out: only when a filter refused every offer a binding could make."""
    usable = [c for c in candidates if c.active and c.same_work and not c.tombstoned]
    if not usable or any(c.filtered is None for c in usable):
        return None
    return min(usable, key=lambda c: (c.rank, c.age)).filtered


class TeamPreference:
    def __init__(self, *, inactive_months: int = DEFAULT_INACTIVE_MONTHS) -> None:
        self._months = inactive_months

    def elect(
        self,
        connection: Connection,
        unit_id: str,
        now: datetime,
        *,
        allowed: Collection[str] | None = None,
    ) -> Election | None:
        """The Variant to ask for: the selection of EF-SRC-037 over the Variants whose binding is
        active, belongs to the Edition's own Work and, when given, is one of `allowed` (the
        bindings the cycle may ask: not tombstoned, not paused, not unloaded). None when none is
        left: the Variant exported so far is then left as it is."""
        u, v = catalog.unit, catalog.unit_variant
        unit = connection.execute(
            sa.select(u.c.edition_id, u.c.number_norm).where(u.c.id == unit_id)
        ).one()
        service = Catalog(connection)
        live = [f for f in service.variant_facts(unit_id) if not f.removed and not f.in_conflict]
        if not live:
            service.set_preferred(unit_id, None)
            return None
        asked = self._asked_variant(connection, unit_id)
        if asked is not None and any(f.id == asked for f in live):
            service.set_preferred(unit_id, asked)  # what was asked for stays the exported one
            return self._election(connection, asked, TeamChoice(None, "already_asked"))
        stats = self._stats(connection, unit.edition_id)
        priority = service.team_priority(unit.edition_id)

        def team(teams: set[TeamKey]) -> TeamChoice:
            return choose_team(
                teams, stats, priority=priority, now=now, inactive_months=self._months
            )

        refused = refusals(connection, unit.edition_id, unit_id, {f.id for f in live})
        candidates = self._candidates(connection, unit.edition_id, live, allowed, refused)
        selected = select(candidates, team)
        # Told on the Unit, so that the series' page says why nothing was asked (EF-SRC-039)
        set_filtered_reason(connection, unit_id, _reason_shown(candidates))
        if selected is None:
            return None
        choice = selected.choice
        chosen = next(f for f in live if f.id == selected.candidate.variant_id)
        previous = connection.execute(
            sa.select(v.c.id, v.c.team_id).where(v.c.unit_id == unit_id, v.c.preferred)
        ).one_or_none()
        service.set_preferred(unit_id, chosen.id)
        if (previous is None or previous.id != chosen.id) and (
            choice.fallback or (previous is not None and previous.team_id != choice.team_id)
        ):  # said when the choice changes: a second election of the same one is silent
            log.warning(
                "team.fallback" if choice.fallback else "team.switched",
                reason=choice.fallback or choice.reason, unit_id=unit_id, number=unit.number_norm,
                edition_id=unit.edition_id, team_id=choice.team_id,
            )  # fmt: skip
        return self._election(connection, chosen.id, choice)

    @staticmethod
    def _candidates(
        connection: Connection,
        edition_id: str,
        live: list[VariantFacts],
        allowed: Collection[str] | None,
        refused: dict[str, str],
    ) -> list[Candidate]:
        b, e = acquisition.source_binding, catalog.edition
        work_id = connection.execute(sa.select(e.c.work_id).where(e.c.id == edition_id)).scalar()
        known = {
            row.id: row
            for row in connection.execute(
                sa.select(b.c.id, b.c.priority, b.c.active, b.c.edition_id, b.c.work_id).where(
                    b.c.id.in_({f.binding_id for f in live})
                )
            )
        }
        out: list[Candidate] = []
        for f in live:
            binding = known.get(f.binding_id)
            if binding is None:
                continue
            out.append(
                Candidate(
                    f.id,
                    f.binding_id,
                    binding.priority,
                    f.team_id,
                    f.age,
                    active=bool(binding.active) and (allowed is None or f.binding_id in allowed),
                    same_work=binding.edition_id == edition_id and binding.work_id == work_id,
                    filtered=refused.get(f.id),
                )
            )
        return out

    @staticmethod
    def _asked_variant(connection: Connection, unit_id: str) -> str | None:
        r, v = acquisition.download_request, catalog.unit_variant
        return connection.execute(
            sa.select(v.c.id)
            .select_from(r.join(v, v.c.id == r.c.variant_id))
            .where(v.c.unit_id == unit_id, r.c.state.in_(_ASKED))
            .order_by(r.c.requested_at)
            .limit(1)
        ).scalar()

    @staticmethod
    def _election(connection: Connection, variant_id: str, choice: TeamChoice) -> Election:
        binding_id = connection.execute(
            sa.select(catalog.unit_variant.c.source_binding_id).where(
                catalog.unit_variant.c.id == variant_id
            )
        ).scalar_one()
        return Election(variant_id, binding_id, choice)

    @staticmethod
    def _stats(connection: Connection, edition_id: str) -> dict[TeamKey, TeamStats]:
        v, u, t = catalog.unit_variant, catalog.unit, catalog.team
        r = acquisition.download_request
        on_disk: dict[TeamKey, int] = {
            row.team_id: row.n
            for row in connection.execute(
                sa.select(v.c.team_id, sa.func.count(sa.distinct(v.c.unit_id)).label("n"))
                .select_from(r.join(v, v.c.id == r.c.variant_id).join(u, u.c.id == v.c.unit_id))
                .where(u.c.edition_id == edition_id, r.c.state == "fetched")
                .group_by(v.c.team_id)
            )
        }
        offered: dict[TeamKey, int] = {
            row.team_id: row.n
            for row in connection.execute(
                sa.select(v.c.team_id, sa.func.count(sa.distinct(v.c.unit_id)).label("n"))
                .select_from(v.join(u, u.c.id == v.c.unit_id))
                .where(u.c.edition_id == edition_id, v.c.removed_at.is_(None))
                .group_by(v.c.team_id)
            )
        }
        last = {
            row.id: row.last_release_at
            for row in connection.execute(
                sa.select(t.c.id, t.c.last_release_at).where(t.c.edition_id == edition_id)
            )
        }
        keys: set[TeamKey] = {*on_disk, *offered}
        return {
            key: TeamStats(on_disk.get(key, 0), last.get(key), offered.get(key, 0)) for key in keys
        }
