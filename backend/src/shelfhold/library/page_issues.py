"""The chapters whose pages could not be normalized (M4 L53, ADR-0054), one row per Unit.

A chapter with a page nothing can read (empty, undecodable, too large, a format with no decoder) or
that kills the decoder stays as received, is left out of every export and shows on the Health page
with the closed code of the first page refused. It is not looked at again at every cycle: the row
holds the hash of the content it was found on, and only `shelfhold library normalize-pages`
(for a decoder that died) or a new content asks again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import storage

__all__ = [
    "PageIssueRow",
    "clear_page_issue",
    "note_page_issue",
    "page_issue_blocks",
    "page_issue_counts",
    "page_issue_for",
]

_issue = storage.page_issue


def note_page_issue(
    engine: Engine, unit_id: str, original_sha256: str, code: str, *, died: bool = False
) -> None:
    """Record `code` for the Unit's content; `died` counts a decoder that was killed by a page."""
    with write_transaction(engine) as connection:
        row = connection.execute(
            sa.select(_issue.c.original_sha256, _issue.c.failures).where(
                _issue.c.unit_id == unit_id
            )
        ).first()
        failures = row.failures if row is not None and row.original_sha256 == original_sha256 else 0
        failures += 1 if died else 0
        values = {
            "original_sha256": original_sha256,
            "code": code,
            "failures": failures,
            "noted_at": sa.func.current_timestamp(),
        }
        if row is None:
            connection.execute(sa.insert(_issue).values(unit_id=unit_id, **values))
        else:
            connection.execute(
                sa.update(_issue).where(_issue.c.unit_id == unit_id).values(**values)
            )


def clear_page_issue(engine: Engine, unit_id: str) -> None:
    with write_transaction(engine) as connection:
        connection.execute(sa.delete(_issue).where(_issue.c.unit_id == unit_id))


class PageIssueRow(NamedTuple):
    original_sha256: str
    code: str
    failures: int
    noted_at: datetime


BACKOFF = timedelta(hours=24)  # per death of the decoder, up to a week
MAX_BACKOFF = timedelta(days=7)


def page_issue_for(connection: Connection, unit_id: str) -> PageIssueRow | None:
    """The Unit's issue, None when it has none."""
    row = connection.execute(
        sa.select(
            _issue.c.original_sha256, _issue.c.code, _issue.c.failures, _issue.c.noted_at
        ).where(_issue.c.unit_id == unit_id)
    ).first()
    if row is None:
        return None
    return PageIssueRow(row.original_sha256, row.code, int(row.failures), row.noted_at)


def page_issue_blocks(
    connection: Connection, unit_id: str, original_sha256: str, now: datetime | None = None
) -> bool:
    """Is this content set aside, not to be decoded again now? A page nothing can read is refused
    for good (for this very content); a decoder that died is asked again after a back-off that
    grows with each death (a day, two, ... a week)."""
    known = page_issue_for(connection, unit_id)
    if known is None or known.original_sha256 != original_sha256:
        return False
    if known.code != "pages.worker_failed":
        return True
    wait = min(BACKOFF * max(known.failures, 1), MAX_BACKOFF)
    return (now or datetime.now(UTC)) < known.noted_at + wait


def page_issue_counts(connection: Connection) -> list[tuple[str, int]]:
    """How many Units hold each code, for the Health page: no title, no path."""
    rows = connection.execute(
        sa.select(_issue.c.code, sa.func.count()).group_by(_issue.c.code).order_by(_issue.c.code)
    )
    return [(str(code), int(count)) for code, count in rows]
