"""The numbers behind `/metrics` (ENF-OBS-002): Prometheus text, read from the database at the
moment of the scrape, with nothing in it that names a person or a title.

What makes it safe is the shape, not a filter: a family has a fixed name, and its only labels are
`state` (taken from a closed list of the schema) and `version`. No label is ever built from a row's
free text: a title, a series, a profile, a user id, a source, a host, a path, an address or an error
message has no way into a name or a label. A count is a number; a state that is not on the list is
dropped, never printed.

Not carried, on purpose: a counter per domain or per source (a host is a name of a place where
content is taken), the scans and the fetch statuses (nothing counts them in this process yet).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.schema import catalog, coherence, identity, platform

__all__ = ["CONTENT_TYPE", "LABEL_NAMES", "Family", "collect", "render", "scrape"]

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
LABEL_NAMES: Final = frozenset({"state", "version"})  # the only labels there will ever be
_FINISHED: Final = ("succeeded", "failed", "dead_letter")
_Sample = tuple[str, Mapping[str, str], float]


@dataclass(frozen=True)
class Family:
    name: str
    kind: str  # `gauge` or `summary`
    help: str
    samples: Sequence[_Sample]


def _by_state(
    connection: Connection, column: sa.Column[str], allowed: Iterable[str]
) -> list[_Sample]:
    counts = dict.fromkeys(allowed, 0)
    rows = connection.execute(sa.select(column, sa.func.count()).group_by(column))
    for state, count in rows:
        if state in counts:  # a value off the list is never a label
            counts[state] = int(count)
    return [("", {"state": state}, float(count)) for state, count in sorted(counts.items())]


def _scalar(connection: Connection, statement: sa.Select[Any]) -> float:
    return float(connection.execute(statement).scalar() or 0)


def collect(connection: Connection, *, version: str) -> list[Family]:
    job = platform.job_view
    seconds = (sa.func.julianday(job.c.finished_at) - sa.func.julianday(job.c.started_at)) * 86_400
    durations = connection.execute(
        sa.select(job.c.status, sa.func.count(), sa.func.coalesce(sa.func.sum(seconds), 0))
        .where(job.c.status.in_(_FINISHED), job.c.started_at.is_not(None))
        .where(job.c.finished_at.is_not(None))
        .group_by(job.c.status)
    )
    timed = {status: (int(n), float(total)) for status, n, total in durations}
    summary: list[_Sample] = []
    for state in _FINISHED:
        n, total = timed.get(state, (0, 0.0))
        summary += [("_count", {"state": state}, float(n)), ("_sum", {"state": state}, total)]
    slots = platform.domain_slot
    return [
        Family(
            "shelfhold_build_info",
            "gauge",
            "Version of this instance.",
            [("", {"version": version}, 1.0)],
        ),
        Family(
            "shelfhold_jobs",
            "gauge",
            "Jobs by state.",
            _by_state(connection, job.c.status, platform.JOB_STATUSES),
        ),
        Family("shelfhold_job_duration_seconds", "summary", "Time jobs took to run.", summary),
        Family(
            "shelfhold_export_plans",
            "gauge",
            "Export plans by state.",
            _by_state(connection, coherence.export_plan.c.status, coherence.PLAN_STATUSES),
        ),
        Family(
            "shelfhold_requests",
            "gauge",
            "Requests by state.",
            _by_state(connection, identity.request.c.state, identity.REQUEST_STATES),
        ),
        Family(
            "shelfhold_rate_limit_slots_in_use",
            "gauge",
            "Concurrency slots held for upstream rate limiting.",
            [
                (
                    "",
                    {},
                    _scalar(
                        connection, sa.select(sa.func.coalesce(sa.func.sum(slots.c.in_use), 0))
                    ),
                )
            ],
        ),
        Family(
            "shelfhold_rate_limit_slots",
            "gauge",
            "Concurrency slots available for rate limiting.",
            [
                (
                    "",
                    {},
                    _scalar(connection, sa.select(sa.func.coalesce(sa.func.sum(slots.c.slots), 0))),
                )
            ],
        ),
        Family(
            "shelfhold_units",
            "gauge",
            "Units in the library.",
            [("", {}, _scalar(connection, sa.select(sa.func.count()).select_from(catalog.unit)))],
        ),
        Family(
            "shelfhold_works",
            "gauge",
            "Works in the catalogue.",
            [("", {}, _scalar(connection, sa.select(sa.func.count()).select_from(catalog.work)))],
        ),
    ]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    return str(int(value)) if value == int(value) else repr(value)


def render(families: Iterable[Family]) -> str:
    """The text exposition format 0.0.4. A label that is not on `LABEL_NAMES` is a bug: refused."""
    lines: list[str] = []
    for family in families:
        lines += [f"# HELP {family.name} {family.help}", f"# TYPE {family.name} {family.kind}"]
        for suffix, labels, value in family.samples:
            if not set(labels) <= LABEL_NAMES:
                raise ValueError("label_not_allowed")
            shown = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
            name = f"{family.name}{suffix}" + (f"{{{shown}}}" if shown else "")
            lines.append(f"{name} {_number(value)}")
    return "\n".join(lines) + "\n"


def scrape(engine: Engine, *, version: str) -> str:
    with engine.connect() as connection:
        return render(collect(connection, version=version))
