"""Progress synchronisation with a Kavita: pull first, push last, last write wins (EF-EXP-026, D11).

For each member linked to a destination (their own key, L12), one pass per Kavita chapter the
member may see:

1. **Pull.** Kavita's page for the member's account is read with the member's key. A page that is
   news becomes a `server` position of that member (`server_state.record_server_state`, the one
   door of an externally held state), a page that is the echo of what Shelfhold itself pushed is
   dropped, a page that has not moved since the last pass is not stored again.
2. **Push.** The member's latest `device` position since the last pass (the **server's** clock,
   never `client_ts`) is written to Kavita, but only forward: a page not beyond Kavita's own (read
   again just before the write) is never sent, and the last page of a chapter is sent as « read »
   only when a traversal marked the Unit read (`reading_progress`), else Kavita is given the page
   before it.

Kavita's own `lastModifiedUtc` is a stamp of **its** clock: it is compared for equality with the
last one seen (« this changed ») and nothing else, so a Kavita whose clock is wrong decides nothing.
What is pulled is dated by Shelfhold's clock.

What one member's pass never does: use another's key, write another's progress, go above a child's
ceiling (a chapter she cannot see is not even asked about), fall back to the service account when
the key is refused, or keep using a key revoked since the pass began (`revoked_at` is read again
before each call). An outage of Kavita ends the pass quietly (`unavailable`): nothing is lost, the
next pass finds the same positions.

The reading context knows nothing of HTTP: `ProgressRemote` is the port the Kavita adapter
(`export.targets.kavita.progress`) implements.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.kavita_names import same_kavita_username
from shelfhold.infrastructure.schema import export, identity, reading
from shelfhold.reading.application.member_keys import MemberKeyRevokedError, live_member_account
from shelfhold.reading.application.progress import (
    UnknownCarrierError,
    _current_hash,
    check_carrier,
)
from shelfhold.reading.application.server_state import record_server_state

log = structlog.get_logger(__name__)

__all__ = [
    "ChapterRef",
    "ChapterRejected",
    "MemberOutcome",
    "MemberRemote",
    "ProgressRemote",
    "RemotePosition",
    "RemoteUnavailable",
    "TokenRefused",
    "TokenUnreadable",
    "server_page",
    "sync_destination",
    "sync_member",
]

EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class RemoteUnavailable(Exception):
    """Kavita cannot be asked now: down, throttled past the cap, a 5xx, no answer."""


class TokenRefused(Exception):
    """Kavita refused the member's key (expired, deleted, demoted): 401 or 403."""


class ChapterRejected(Exception):
    """Kavita will not give or take this one chapter for the account (no access, unknown, an answer
    that is not a progress): the chapter is skipped, the pass goes on."""


class TokenUnreadable(Exception):
    """The sealed key cannot be opened for this row (moved, corrupted, unknown key id)."""


@dataclass(frozen=True)
class ChapterRef:
    """A chapter of Kavita and the Shelfhold file (export item) it holds."""

    chapter_id: int
    volume_id: int
    series_id: int
    library_id: int
    pages: int  # Kavita's page count: reading it all is `pageNum == pages`
    carrier_id: str


@dataclass(frozen=True)
class RemotePosition:
    page: int  # 0 when Kavita holds nothing for the account
    modified: str | None  # Kavita's own stamp, opaque: compared for equality only


class MemberRemote(Protocol):
    """One member's key against Kavita (the adapter retries and caps; it raises
    `RemoteUnavailable` or `TokenRefused`)."""

    def account_name(self) -> str: ...

    def get(self, chapter: ChapterRef) -> RemotePosition: ...

    def save(self, chapter: ChapterRef, page: int) -> None: ...


class ProgressRemote(Protocol):
    def chapters(self, destination_id: str) -> Sequence[ChapterRef]:
        """The chapters of the destination's files (structure: nothing of any account)."""
        ...

    def member(self, user_id: str, destination_id: str) -> MemberRemote:
        """The member's key opened now (`MemberKeyRevokedError`, `TokenUnreadable`)."""
        ...


@dataclass
class MemberOutcome:
    """How a member's pass ended: `ok`, or a closed code that says why it stopped."""

    code: str
    pulled: int = 0
    pushed: int = 0
    echoes: int = 0
    hidden: int = 0
    rejected: int = 0


CODES: Final = (
    "ok", "not_enabled", "member_key_revoked", "token_unreadable", "token_refused",
    "account_mismatch", "account_shared", "unavailable",
)  # fmt: skip


class _Stop(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def server_page(page: int, pages: int) -> int:
    """The page a Kavita state is recorded at. Kavita says « read » with `page == pages`; any page
    short of it is a position, and is kept two pages short of the end at most, so that a Kavita
    sitting on its last page index (not finished) is never taken for a reading, whichever way the
    pages of a file are counted."""
    if pages <= 0:
        return max(page, 0)
    return pages if page >= pages else min(page, max(pages - 2, 0))


def push_page(page: int, pages: int, *, finished: bool) -> int:
    """The page written to Kavita for a local page: never the count (Kavita's « read ») unless the
    Unit was marked read by a traversal."""
    if pages <= 0:
        return max(page, 0)
    if finished and page >= pages - 1:
        return pages
    return min(max(page, 0), pages - 1)


# ----------------------------------------------------------------------------- entry points


def sync_member(
    engine: sa.Engine,
    remote: ProgressRemote,
    *,
    user_id: str,
    destination_id: str,
    clock: Any,
    chapters: Sequence[ChapterRef] | None = None,
) -> MemberOutcome:
    """One pass for one member. Never raises for what Kavita or the key does: it says why."""
    outcome = MemberOutcome("ok")
    try:
        _check_enabled(engine, destination_id)
        if chapters is None:
            chapters = _chapters(remote, destination_id)
        _pass(engine, remote, user_id, destination_id, clock, chapters, outcome)
    except _Stop as stop:
        outcome.code = stop.code
    log.info(
        "progress_sync.member_done", code=outcome.code, pulled=outcome.pulled,
        pushed=outcome.pushed, echoes=outcome.echoes, hidden=outcome.hidden,
    )  # fmt: skip
    return outcome


def sync_destination(
    engine: sa.Engine, remote: ProgressRemote, *, destination_id: str, clock: Any
) -> dict[str, MemberOutcome]:
    """Every member with a live key on the destination, one after the other. A member whose key
    stops does not stop the others; a Kavita that cannot be asked stops them all, quietly."""
    try:
        _check_enabled(engine, destination_id)
    except _Stop:
        return {}
    members = _linked_members(engine, destination_id)
    if not members:
        return {}
    try:
        chapters = _chapters(remote, destination_id)
    except _Stop as stop:
        return {user: MemberOutcome(stop.code) for user in members}
    results: dict[str, MemberOutcome] = {}
    down = False
    for user in members:
        if down:
            results[user] = MemberOutcome("unavailable")
            continue
        results[user] = sync_member(
            engine, remote, user_id=user, destination_id=destination_id, clock=clock,
            chapters=chapters,
        )  # fmt: skip
        down = results[user].code == "unavailable"
    return results


# ----------------------------------------------------------------------------- one pass


def _check_enabled(engine: sa.Engine, destination_id: str) -> None:
    table, target = export.shelf_destination, export.export_target
    with engine.connect() as connection:
        row = connection.execute(
            sa.select(table.c.sync_progress, target.c.kind)
            .join(target, target.c.id == table.c.target_id)
            .where(table.c.id == destination_id)
        ).first()
    if row is None or not row.sync_progress or row.kind != "kavita":
        raise _Stop("not_enabled")


def _chapters(remote: ProgressRemote, destination_id: str) -> Sequence[ChapterRef]:
    try:
        return remote.chapters(destination_id)
    except RemoteUnavailable:
        raise _Stop("unavailable") from None


def _linked_members(engine: sa.Engine, destination_id: str) -> list[str]:
    account = identity.destination_account
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(account.c.user_id)
            .where(
                account.c.shelf_destination_id == destination_id,
                account.c.revoked_at.is_(None),
                account.c.secret_ct.is_not(None),
            )
            .order_by(account.c.created_at, account.c.id)
        )
        return [str(r.user_id) for r in rows]


def _live(engine: sa.Engine, user_id: str, destination_id: str) -> str:
    """The id of the member's live account, read now (a revocation is seen at the next call)."""
    with engine.connect() as connection:
        try:
            return live_member_account(
                connection, user_id=user_id, shelf_destination_id=destination_id
            )
        except MemberKeyRevokedError:
            raise _Stop("member_key_revoked") from None


def _declared_name(engine: sa.Engine, account_id: str) -> str:
    account = identity.destination_account
    with engine.connect() as connection:
        name = connection.execute(
            sa.select(account.c.remote_user).where(account.c.id == account_id)
        ).scalar()
    return "" if name is None else str(name)


def _shared(engine: sa.Engine, user_id: str, destination_id: str, name: str) -> bool:
    """Is this Kavita account also the live account of another profile, on any binding of the same
    Kavita? Then its progress belongs to neither, and neither is synchronised."""
    account, binding = identity.destination_account, export.shelf_destination
    with engine.connect() as connection:
        target = connection.execute(
            sa.select(binding.c.target_id).where(binding.c.id == destination_id)
        ).scalar()
        others = connection.execute(
            sa.select(account.c.remote_user)
            .join(binding, binding.c.id == account.c.shelf_destination_id)
            .where(
                binding.c.target_id == target,
                account.c.user_id != user_id,
                account.c.revoked_at.is_(None),
                account.c.secret_ct.is_not(None),
            )
        ).all()
    return any(same_kavita_username(str(o.remote_user), name) for o in others)


def _open(
    engine: sa.Engine, remote: ProgressRemote, user_id: str, destination_id: str
) -> MemberRemote:
    _live(engine, user_id, destination_id)
    try:
        return remote.member(user_id, destination_id)
    except MemberKeyRevokedError:
        raise _Stop("member_key_revoked") from None
    except TokenUnreadable:
        raise _Stop("token_unreadable") from None


def _pass(
    engine: sa.Engine,
    remote: ProgressRemote,
    user_id: str,
    destination_id: str,
    clock: Any,
    chapters: Sequence[ChapterRef],
    outcome: MemberOutcome,
) -> None:
    started = clock.now()
    account_id = _live(engine, user_id, destination_id)
    declared = _declared_name(engine, account_id)
    if _shared(engine, user_id, destination_id, declared):
        raise _Stop("account_shared")
    try:
        who = _open(engine, remote, user_id, destination_id).account_name()
    except TokenRefused:
        raise _Stop("token_refused") from None
    except (RemoteUnavailable, ChapterRejected):
        raise _Stop("unavailable") from None
    if not same_kavita_username(who, declared):
        raise _Stop("account_mismatch")  # the key opens another account than the one on file
    for ref in chapters:
        if not _visible(engine, user_id, ref):
            outcome.hidden += 1
            continue
        try:
            _chapter(engine, remote, user_id, destination_id, account_id, ref, clock, started,
                     outcome)  # fmt: skip
        except ChapterRejected:
            outcome.rejected += 1  # this chapter only: the account may not open it, or it is gone
        except TokenRefused:
            raise _Stop("token_refused") from None
        except RemoteUnavailable:
            raise _Stop("unavailable") from None
        except MemberKeyRevokedError:
            raise _Stop("member_key_revoked") from None
    _note_pass(engine, user_id, destination_id, started, pushed=outcome.pushed > 0)


def _visible(engine: sa.Engine, user_id: str, ref: ChapterRef) -> bool:
    """A chapter above the member's ceiling is neither asked about nor written: it is not looked
    at, so nothing of it can be stored."""
    with engine.connect() as connection:
        try:
            check_carrier(connection, user_id, "export_item", ref.carrier_id)
        except UnknownCarrierError:
            return False
    return True


@dataclass(frozen=True)
class _Note:
    pushed_page: int | None
    seen_page: int | None
    seen_modified: str | None
    synced_at: datetime


def _load_note(
    engine: sa.Engine, user_id: str, destination_id: str, chapter_id: int, account_id: str
) -> _Note | None:
    note = reading.progress_sync_note
    with engine.connect() as connection:
        row = connection.execute(
            sa.select(note).where(
                note.c.shelf_destination_id == destination_id,
                note.c.user_id == user_id,
                note.c.kavita_chapter_id == chapter_id,
            )
        ).first()
    if row is None or row.account_id != account_id:  # a re-linked account starts afresh
        return None
    synced = row.synced_at if row.synced_at.tzinfo else row.synced_at.replace(tzinfo=UTC)
    return _Note(row.pushed_page, row.seen_page, row.seen_modified, synced)


def _save_note(
    engine: sa.Engine, user_id: str, destination_id: str, account_id: str, ref: ChapterRef,
    *, pushed_page: int | None, pushed_at: datetime | None, seen_page: int | None,
    seen_modified: str | None, synced_at: datetime,
) -> None:  # fmt: skip
    note = reading.progress_sync_note
    values: dict[str, Any] = {
        "shelf_destination_id": destination_id, "user_id": user_id,
        "kavita_chapter_id": ref.chapter_id, "account_id": account_id,
        "carrier_id": ref.carrier_id, "pushed_page": pushed_page, "pushed_at": pushed_at,
        "seen_page": seen_page, "seen_modified": seen_modified, "synced_at": synced_at,
    }  # fmt: skip
    statement = sqlite_insert(note).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=["shelf_destination_id", "user_id", "kavita_chapter_id"],
        set_={k: v for k, v in values.items() if k not in {"shelf_destination_id", "user_id",
                                                           "kavita_chapter_id"}},
    )  # fmt: skip
    with write_transaction(engine) as connection:
        connection.execute(statement)


def _chapter(
    engine: sa.Engine,
    remote: ProgressRemote,
    user_id: str,
    destination_id: str,
    account_id: str,
    ref: ChapterRef,
    clock: Any,
    started: datetime,
    outcome: MemberOutcome,
) -> None:
    note = _load_note(engine, user_id, destination_id, ref.chapter_id, account_id)
    # ---- pull
    member = _open(engine, remote, user_id, destination_id)
    _live(engine, user_id, destination_id)
    seen = member.get(ref)
    pushed_page = note.pushed_page if note else None
    echo = pushed_page is not None and seen.page == pushed_page
    unchanged = (
        note is not None and note.seen_page == seen.page and note.seen_modified == seen.modified
    )
    if echo:
        outcome.echoes += 1
    elif (
        seen.page > 0
        and not unchanged
        and _record_pull(engine, user_id, destination_id, ref, seen.page, clock)
    ):
        outcome.pulled += 1
    if not echo:
        pushed_page = None  # Kavita moved on its own: what we pushed is no longer what it holds
    # ---- push
    wanted = _pending_page(engine, user_id, ref, note.synced_at if note else EPOCH)
    pushed_at = None
    if wanted is not None and wanted > seen.page:
        _live(engine, user_id, destination_id)
        member = _open(engine, remote, user_id, destination_id)
        latest = member.get(ref)  # read again just before writing: never back from what it holds
        if wanted > latest.page:
            member.save(ref, wanted)
            outcome.pushed += 1
            pushed_page, pushed_at = wanted, clock.now()
            seen = RemotePosition(wanted, None)
    _save_note(
        engine, user_id, destination_id, account_id, ref,
        pushed_page=pushed_page, pushed_at=pushed_at, seen_page=seen.page,
        seen_modified=seen.modified, synced_at=started,
    )  # fmt: skip


def _record_pull(
    engine: sa.Engine, user_id: str, destination_id: str, ref: ChapterRef, page: int, clock: Any
) -> bool:
    now = clock.now()
    at = server_page(page, ref.pages)
    with write_transaction(engine) as connection:
        content_hash = _current_hash(connection, "export_item", ref.carrier_id)
        if content_hash is None:  # a file with no content to anchor a position on
            return False
        record_server_state(
            connection, user_id=user_id, shelf_destination_id=destination_id,
            carrier_type="export_item", carrier_id=ref.carrier_id, content_hash=content_hash,
            locator={"page": at}, percent=min(at / ref.pages, 1.0) if ref.pages > 0 else None,
            client_ts=now, now=now,
        )  # fmt: skip
    return True


def _pending_page(engine: sa.Engine, user_id: str, ref: ChapterRef, since: datetime) -> int | None:
    """What to say to Kavita: the page of the member's **latest** device position since the last
    pass (the server's clock, `id` for a tie), on the content the file carries now."""
    position = reading.reading_position
    with engine.connect() as connection:
        current = _current_hash(connection, "export_item", ref.carrier_id)
        row = connection.execute(
            sa.select(position.c.content_hash, position.c.locator, position.c.percent)
            .where(
                position.c.user_id == user_id,
                position.c.carrier_type == "export_item",
                position.c.carrier_id == ref.carrier_id,
                position.c.source == "device",
                position.c.server_ts >= since,
            )
            .order_by(position.c.server_ts.desc(), position.c.id.desc())
            .limit(1)
        ).first()
        if row is None or current is None or row.content_hash != current:
            return None  # nothing new, or a page counted in a file that is no longer there
        page = row.locator.get("page") if isinstance(row.locator, dict) else None
        if not isinstance(page, int) or isinstance(page, bool):
            if row.percent is None or ref.pages <= 0:
                return None
            page = int(row.percent * ref.pages)
        finished = _all_read(connection, user_id, ref.carrier_id)
    return push_page(page, ref.pages, finished=finished)


def _all_read(connection: sa.Connection, user_id: str, carrier_id: str) -> bool:
    cover, view = (
        export.unit_coverage,
        sa.table(
            "reading_progress", sa.column("user_id"), sa.column("unit_id"), sa.column("state")
        ),
    )
    units = [
        str(r.unit_id)
        for r in connection.execute(
            sa.select(cover.c.unit_id).where(
                cover.c.carrier_type == "export_item", cover.c.carrier_id == carrier_id
            )
        )
    ]
    if not units:
        return False
    read = connection.execute(
        sa.select(sa.func.count()).where(
            view.c.user_id == user_id, view.c.unit_id.in_(units), view.c.state == "read"
        )
    ).scalar()
    return read == len(units)


def _note_pass(
    engine: sa.Engine, user_id: str, destination_id: str, started: datetime, *, pushed: bool
) -> None:
    cursor = reading.progress_sync_cursor
    values: dict[str, Any] = {
        "shelf_destination_id": destination_id,
        "user_id": user_id,
        "last_pull_server_ts": started,
    }
    if pushed:
        values["last_push_at"] = started
    statement = sqlite_insert(cursor).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=["shelf_destination_id", "user_id"],
        set_={k: v for k, v in values.items() if k not in {"shelf_destination_id", "user_id"}},
    )
    with write_transaction(engine) as connection:
        connection.execute(statement)
