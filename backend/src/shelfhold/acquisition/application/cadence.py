"""Adaptive check cadence (EF-SRC-038): how often a followed series is looked at.

The wait before the next look follows the observed rhythm of the series (the median gap between
its discoveries, the same cadence `series.stale` uses), never under `follow.check_min` (1 h) nor
over `follow.check_max` (7 days). A series the upstream says is finished or paused, or that the
admin marked completed, is looked at once per `follow.check_finished` (30 days) at most. "Check
now" makes one series due at the next look, as a job of the news class: the limit by upstream
source (`limit_key`, the spacing and the jitter of the refresh) still applies, because the look is
the follow cycle itself.

In always-on mode Shelfhold refreshes nothing: `CadencedFollowCycle` leaves the cycle as it is and
the cadence only feeds `series.stale`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from shelfhold.acquisition.application.follow import (
    FOLLOW_JOB_KIND,
    FollowCycle,
    FollowReport,
    _Binding,
    follow_dedupe_key,
)
from shelfhold.acquisition.application.staleness import (
    FINISHED_STATUSES,
    NOT_CHECKED,
    discoveries_of,
)
from shelfhold.acquisition.domain.freshness import cadence
from shelfhold.infrastructure.schema import acquisition, catalog

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shelfhold.config import Settings

__all__ = [
    "CadencePolicy",
    "CadencedFollowCycle",
    "SeriesPausedError",
    "UnknownSeriesError",
    "check_interval",
    "check_now",
    "make_all_due",
    "next_check",
    "request_check_job",
]

UNKNOWN_CADENCE = timedelta(days=1)  # a series with no measurable rhythm yet
OVERDUE_DIVISOR = 4  # an overdue series is looked at again after a quarter of its interval
RARE_UPSTREAM = ("finished", "paused")  # edition.upstream_status


class UnknownSeriesError(LookupError):
    """No followed series has this edition."""


class SeriesPausedError(ValueError):
    """The series is paused by hand: nothing is asked of its source, not even on request."""


@dataclass(frozen=True)
class CadencePolicy:
    check_min: timedelta = timedelta(hours=1)
    check_max: timedelta = timedelta(days=7)
    check_finished: timedelta = timedelta(days=30)

    @classmethod
    def from_settings(cls, settings: Settings) -> CadencePolicy:
        return cls(
            timedelta(seconds=settings.follow_check_min_s),
            timedelta(seconds=settings.follow_check_max_s),
            timedelta(seconds=settings.follow_check_finished_s),
        )

    def clamp(self, wait: timedelta) -> timedelta:
        return min(max(wait, self.check_min), self.check_max)


def check_interval(observed: timedelta | None, policy: CadencePolicy) -> timedelta:
    """The expected gap between two releases, bounded by the policy."""
    return policy.clamp(observed if observed is not None else UNKNOWN_CADENCE)


def next_check(
    now: datetime, discoveries: Sequence[datetime], policy: CadencePolicy, *, finished: bool
) -> datetime:
    """When the series is next looked at: around its next expected release, within the bounds."""
    if finished:
        return now + policy.check_finished
    interval = check_interval(cadence(discoveries), policy)
    if not discoveries:
        return now + interval
    expected = max(discoveries) + interval
    if expected <= now:  # late: look again soon rather than wait a whole interval
        return now + policy.clamp(interval / OVERDUE_DIVISOR)
    return now + policy.clamp(expected - now)


def _rarely_checked(connection: sa.Connection, edition_id: str, override: str | None) -> bool:
    if override in NOT_CHECKED:
        return True
    e, w = catalog.edition, catalog.work
    row = connection.execute(
        sa.select(e.c.upstream_status, w.c.status)
        .select_from(e.join(w, w.c.id == e.c.work_id))
        .where(e.c.id == edition_id)
    ).first()
    return row is not None and (
        row.upstream_status in RARE_UPSTREAM or row.status in FINISHED_STATUSES
    )


def check_now(
    connection: sa.Connection, edition_id: str, now: datetime, policy: CadencePolicy | None = None
) -> list[str]:
    """Make the series due at the next look; the plugins (services) to look through are returned.
    A series paused by hand is refused: it is never looked at. A series whose source was asked
    less than `check_min` ago is left alone and `[]` is returned: ten clicks are one look."""
    b, si, f = acquisition.source_binding, acquisition.source_item, acquisition.series_follow
    rows = connection.execute(
        sa.select(b.c.id, si.c.plugin_id, f.c.check_override, b.c.last_refreshed_at)
        .select_from(
            b.join(si, si.c.id == b.c.source_item_id).join(f, f.c.edition_id == b.c.edition_id)
        )
        .where(b.c.edition_id == edition_id, b.c.active)
    ).all()
    if not rows:
        raise UnknownSeriesError(edition_id)
    if rows[0].check_override == "paused":
        raise SeriesPausedError(edition_id)
    floor = (policy or CadencePolicy()).check_min
    fresh = [r.last_refreshed_at is not None and now - r.last_refreshed_at < floor for r in rows]
    if all(fresh):
        return []
    connection.execute(
        sa.update(b).where(b.c.id.in_([r.id for r in rows])).values(next_expected=now)
    )
    return sorted({str(r.plugin_id) for r in rows})


def make_all_due(connection: sa.Connection, now: datetime) -> None:
    """An admin who runs the follow job by hand asks for a look at every series that is being
    followed actively: the cadence is for the periodic pace, not for a request. Finished and
    paused series keep their rare pace."""
    b, f = acquisition.source_binding, acquisition.series_follow
    e, w = catalog.edition, catalog.work
    rare = (
        sa.select(f.c.edition_id)
        .select_from(f.join(e, e.c.id == f.c.edition_id).join(w, w.c.id == e.c.work_id))
        .where(
            sa.or_(
                f.c.check_override.in_(NOT_CHECKED),
                e.c.upstream_status.in_(RARE_UPSTREAM),
                w.c.status.in_(FINISHED_STATUSES),
            )
        )
    )
    connection.execute(
        sa.update(b).where(b.c.active, b.c.edition_id.notin_(rare)).values(next_expected=now)
    )


def request_check_job(
    engine: sa.Engine, clock: Any, pools: Any, edition_id: str, policy: CadencePolicy | None = None
) -> list[str]:
    """ "Check now": the series is made due and the follow job of its service is queued in the
    news class (a double click joins the job in flight). `[]`: asked too recently, nothing done."""
    with engine.begin() as connection:
        plugins = check_now(connection, edition_id, clock.now(), policy)
    for plugin_id in plugins:
        pools.submit(
            FOLLOW_JOB_KIND, dedupe_key=follow_dedupe_key(plugin_id), source_key=plugin_id,
            priority_class="new",
        )  # fmt: skip
    return plugins


class CadencedFollowCycle(FollowCycle):
    """The follow cycle whose refresh (piloted mode) only reaches the series that are due."""

    def __init__(self, *args: Any, policy: CadencePolicy | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._policy = policy or CadencePolicy()
        self._due: set[str] = set()
        self._before: dict[str, tuple[_Binding, int]] = {}

    async def run(self) -> FollowReport:
        self._due, self._before = set(), {}
        report = await super().run()
        if self._piloted:
            self._reschedule()
        return report

    async def _refresh(self, followed: list[_Binding], report: FollowReport) -> None:
        now = self._clock.now()
        with self._engine.connect() as connection:
            due = [b for b in followed if self._is_due(connection, b, now)]
            self._before = {
                b.id: (b, len(discoveries_of(connection, b.edition_id))) for b in followed
            }
        self._due = {b.id for b in due}
        await super()._refresh(due, report)

    def _is_due(self, connection: sa.Connection, binding: _Binding, now: datetime) -> bool:
        table = acquisition.source_binding
        expected = connection.execute(
            sa.select(table.c.next_expected).where(table.c.id == binding.id)
        ).scalar_one()
        farthest = now + max(self._policy.check_max, self._policy.check_finished)
        return expected is None or expected <= now or expected > farthest  # a clock set back

    def _reschedule(self) -> None:
        """After the look: the series that were refreshed, or that showed a new release, get
        their next look from what is known now."""
        now = self._clock.now()
        table = acquisition.source_binding
        with self._engine.begin() as connection:
            for binding_id, (binding, before) in self._before.items():
                seen = discoveries_of(connection, binding.edition_id)
                if binding_id not in self._due and len(seen) == before:
                    continue
                finished = _rarely_checked(connection, binding.edition_id, binding.check_override)
                when = next_check(now, seen, self._policy, finished=finished)
                values: dict[str, Any] = {"next_expected": when}
                if binding_id in self._due:  # the source was asked in this cycle
                    values["last_refreshed_at"] = now
                connection.execute(
                    sa.update(table).where(table.c.id == binding_id).values(**values)
                )
