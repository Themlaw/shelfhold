"""The read state pushed back to Suwayomi (M4 L17, EF-SRC-026, D12).

Suwayomi has one user and no profiles, so exactly one profile of Shelfhold speaks for it: the
**owner**, the setting `source.read_state_owner` (a user name), the administrator when it is empty.
A child is never the owner and never pushed: a setting that names one stops the push
(`owner_is_child`) and no other profile takes over. A member who is not the owner is not pushed
either.

What is pushed is read from `reading_progress`, the one view that already knows what « read »
means (a plausible traversal, or a state a server holds and that is not the echo of Shelfhold's own
push; a look at the last page is never « read ») and from nothing else. Nothing is ever read back
from Suwayomi's `isRead`: no echo to drop, no loop with the check of the updates (piloted mode: the
push asks Suwayomi for no refresh).

Cost. `read_state_push_note` is the cursor: one row per owner, binding and chapter, the state last
pushed. The pass is the difference between the view and the notes, computed in the database: with
nothing new not one request leaves. Per series with news, one listing of its chapters (to skip what
Suwayomi already says), then one request per batch of chapters, `read` and `unread` apart. Only
chapters Shelfhold holds a readable copy of are pushed (what Suwayomi downloads or keeps does not
change for the others). An undone mark (`undo_read_mark`) is pushed back as « unread », but only
for a chapter Shelfhold itself pushed as read: Suwayomi's own user's readings are never undone here.

A service asleep behind a WakeHook is not woken for this: the pass is `asleep`, the next one pushes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import identity, platform, reading
from shelfhold.library.public import has_readable_copy

log = structlog.get_logger(__name__)

__all__ = [
    "BATCH_SIZE",
    "OWNER_KEY",
    "PushReport",
    "ReadStateRemote",
    "RemoteChapter",
    "RemoteRefused",
    "RemoteUnavailable",
    "push_read_state",
    "resolve_owner",
]

OWNER_KEY: Final = "source.read_state_owner"
BATCH_SIZE: Final = 100  # chapters per `updateChapters`: a long series never makes a huge request
PLUGIN: Final = "suwayomi"


class RemoteUnavailable(Exception):
    """Suwayomi cannot be asked now: down, asleep, throttled, a 5xx."""


class RemoteRefused(Exception):
    """Suwayomi answered and refused (credentials, schema, series gone): the class, no message."""


@dataclass(frozen=True)
class RemoteChapter:
    chapter_id: int
    is_read: bool


class ReadStateRemote(Protocol):
    async def chapters(self, source_key: str, remote_id: str) -> dict[str, RemoteChapter]:
        """The chapters of one series by unit key (`source_key:chapter url`)."""
        ...

    async def set_read(self, chapter_ids: Sequence[int], *, read: bool) -> None: ...


@dataclass
class PushReport:
    """`code`: `ok`, `owner_is_child`, `owner_unknown`, `asleep`, `unavailable`, `refused`,
    `stopped`. The counts are what was done before it ended."""

    code: str = "ok"
    pushed_read: int = 0
    pushed_unread: int = 0
    already: int = 0  # Suwayomi already said so: noted, not written
    series: int = 0  # series Suwayomi was asked about
    missing: int = 0  # series Suwayomi no longer knows: left alone, asked again at the next pass


@dataclass(frozen=True)
class _Want:
    binding_id: str
    source_key: str
    remote_id: str
    remote_unit_id: str
    unit_id: str


def resolve_owner(connection: sa.Connection) -> tuple[str | None, str]:
    """`(user id, "ok")`, or `(None, code)`. Never a child, never a fallback to another profile."""
    user = identity.user
    stored = connection.execute(
        sa.select(platform.app_setting.c.value).where(platform.app_setting.c.key == OWNER_KEY)
    ).scalar()
    if stored:
        row = connection.execute(
            sa.select(user.c.id, user.c.role, user.c.max_age_rating).where(
                user.c.username == str(stored)
            )
        ).first()
        if row is None:
            return None, "owner_unknown"
    else:
        row = connection.execute(
            sa.select(user.c.id, user.c.role, user.c.max_age_rating)
            .where(user.c.role == "admin")
            .order_by(user.c.created_at, user.c.id)
            .limit(1)
        ).first()
        if row is None:
            return None, "owner_unknown"
    if row.role == "child" or row.max_age_rating is not None:
        return None, "owner_is_child"
    return str(row.id), "ok"


_TO_READ = sa.text(
    """
SELECT b.id AS binding_id, i.source_key, i.remote_id, v.remote_unit_id, v.unit_id
FROM source_binding b
JOIN source_item i ON i.id = b.source_item_id AND i.plugin_id = :plugin
JOIN unit_variant v ON v.source_binding_id = b.id AND v.removed_at IS NULL
JOIN reading_progress rp ON rp.unit_id = v.unit_id AND rp.user_id = :owner AND rp.state = 'read'
LEFT JOIN read_state_push_note n ON n.user_id = :owner AND n.source_binding_id = b.id
  AND n.remote_unit_id = v.remote_unit_id
WHERE b.active AND (n.state IS NULL OR n.state <> 'read')
ORDER BY b.id, v.remote_unit_id
"""
)
_TO_UNREAD = sa.text(
    """
SELECT b.id AS binding_id, i.source_key, i.remote_id, n.remote_unit_id, n.unit_id
FROM source_binding b
JOIN source_item i ON i.id = b.source_item_id AND i.plugin_id = :plugin
JOIN read_state_push_note n ON n.source_binding_id = b.id AND n.user_id = :owner
  AND n.state = 'read'
WHERE b.active AND NOT EXISTS (
  SELECT 1 FROM reading_progress rp
  WHERE rp.user_id = :owner AND rp.unit_id = n.unit_id AND rp.state = 'read')
ORDER BY b.id, n.remote_unit_id
"""
)


def _to_read(connection: sa.Connection, owner: str) -> list[_Want]:
    rows = connection.execute(_TO_READ, {"plugin": PLUGIN, "owner": owner})
    return [_Want(*row) for row in rows if has_readable_copy(connection, str(row.unit_id))]


def _to_unread(connection: sa.Connection, owner: str) -> list[_Want]:
    rows = connection.execute(_TO_UNREAD, {"plugin": PLUGIN, "owner": owner})
    return [_Want(*row) for row in rows]


def _note(engine: sa.Engine, owner: str, wants: Sequence[_Want], state: str, clock: Clock) -> None:
    table = reading.read_state_push_note
    with write_transaction(engine) as connection:
        for want in wants:
            connection.execute(
                sqlite_insert(table)
                .values(
                    user_id=owner, source_binding_id=want.binding_id,
                    remote_unit_id=want.remote_unit_id, unit_id=want.unit_id, state=state,
                    pushed_at=clock.now(),
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "source_binding_id", "remote_unit_id"],
                    set_={"state": state, "unit_id": want.unit_id, "pushed_at": clock.now()},
                )
            )  # fmt: skip


async def push_read_state(
    engine: sa.Engine,
    remote: ReadStateRemote,
    *,
    clock: Clock,
    awake: Callable[[], bool] = lambda: True,
    should_stop: Callable[[], bool] = lambda: False,
    batch_size: int = BATCH_SIZE,
) -> PushReport:
    report = PushReport()
    with engine.connect() as connection:
        owner, code = resolve_owner(connection)
        if owner is None:
            report.code = code
            log.info("read_state_push.owner", code=code)
            return report
        wanted: dict[str, list[tuple[_Want, bool]]] = defaultdict(list)  # binding -> (want, read)
        for want in _to_read(connection, owner):
            wanted[want.binding_id].append((want, True))
        for want in _to_unread(connection, owner):
            wanted[want.binding_id].append((want, False))
    if not wanted:
        return report  # nothing new: not one request
    if not awake():
        report.code = "asleep"  # never woken for this: the next pass goes on
        return report
    for wants in wanted.values():
        if should_stop():
            report.code = "stopped"
            return report
        try:
            await _push_series(engine, remote, owner, wants, report, clock, batch_size)
        except RemoteUnavailable:
            report.code = "unavailable"  # nothing is lost: the notes follow what was written
            return report
        except RemoteRefused as error:
            if str(error) == "content":  # this series is gone from Suwayomi: the others go on
                report.missing += 1
                continue
            report.code = "refused"
            log.warning("read_state_push.refused", failure_class=str(error))
            return report
    return report


async def _push_series(
    engine: sa.Engine,
    remote: ReadStateRemote,
    owner: str,
    wants: Sequence[tuple[_Want, bool]],
    report: PushReport,
    clock: Clock,
    batch_size: int,
) -> None:
    first = wants[0][0]
    listed = await remote.chapters(first.source_key, first.remote_id)
    report.series += 1
    for read in (True, False):
        pending: list[tuple[_Want, int]] = []
        settled: list[_Want] = []
        for want, wants_read in wants:
            chapter = listed.get(want.remote_unit_id)
            if wants_read is not read:
                continue
            if chapter is None:
                if not read:
                    settled.append(want)  # gone from Suwayomi: nothing left to undo
                continue  # to read: left alone, asked again with the series (a limit, documented)
            if chapter.is_read == read:
                settled.append(want)
            else:
                pending.append((want, chapter.chapter_id))
        if settled:
            _note(engine, owner, settled, "read" if read else "unread", clock)
            report.already += len(settled)
        pending.sort(key=lambda pair: pair[1])
        for start in range(0, len(pending), batch_size):
            chunk = pending[start : start + batch_size]
            await remote.set_read([chapter_id for _, chapter_id in chunk], read=read)
            _note(engine, owner, [want for want, _ in chunk], "read" if read else "unread", clock)
            if read:
                report.pushed_read += len(chunk)
            else:
                report.pushed_unread += len(chunk)
