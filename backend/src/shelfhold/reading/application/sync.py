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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.kavita_names import same_kavita_username
from shelfhold.infrastructure.schema import export, identity, reading
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.reading.application.member_keys import MemberKeyRevokedError, live_member_account
from shelfhold.reading.application.page_map import relayable, verify_page_maps
from shelfhold.reading.application.progress import (
    UnknownCarrierError,
    _current_hash,
    check_carrier,
)
from shelfhold.reading.application.server_state import record_server_state
from shelfhold.reading.bundle import store

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_MAX_SESSION_HOURS",
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

    def series_stamps(self, library_ids: Sequence[int]) -> dict[int, str]:
        """What the account's series list says of each series (opaque, compared for equality):
        empty when it cannot be read whole, and then every chapter is asked."""
        ...

    def close(self) -> None:
        """End the pass: the connection is closed."""
        ...

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
    unverified: int = 0  # chapters whose page of the block was not handed to Kavita (L50)


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
    Unit was marked read by a traversal, and without one never past `pages - 2`, the cap
    `server_page` applies on the way back. A KOReader that opens Kavita's page stream asks for the
    image after the saved page, and Kavita adds one to a page within two of the end
    (`OPDSController`): a look pushed at `pages - 1` would come back as « read » unturned."""
    if pages <= 0:
        return max(page, 0)
    if finished and page >= pages - 1:
        return pages
    return min(max(page, 0), max(pages - 2, 0))


DEFAULT_MAX_SESSION_HOURS: Final = 24.0  # `reading.relay_max_session_hours`


# ----------------------------------------------------------------------------- entry points


def sync_member(
    engine: sa.Engine,
    remote: ProgressRemote,
    *,
    user_id: str,
    destination_id: str,
    clock: Any,
    chapters: Sequence[ChapterRef] | None = None,
    should_stop: Callable[[], bool] | None = None,
    page_map_keyring: Callable[[], Keyring] | None = None,
    max_session_hours: float = DEFAULT_MAX_SESSION_HOURS,
    mode: str = "sync",
) -> MemberOutcome:
    """One pass for one member. Never raises for what Kavita or the key does: it says why.

    `page_map_keyring` is what compares the page counts of Kavita with the page map of the blocks
    (`page_map.verify_page_maps`): without it no member of a block is `verified`, and no page of a
    block is handed to Kavita.

    `mode` is for a rename plan (L16), which asks for its two halves apart: `"pull"` reads and
    writes nothing in Kavita (before the move: what only Kavita holds comes first), `"restore"`
    pushes the member's **furthest proven** page on every chapter of `chapters` (after the scan:
    Kavita has deleted the readings of the series it lost). Default `"sync"`: the periodic pass."""
    outcome = MemberOutcome("ok")
    try:
        _check_enabled(engine, destination_id)
        if chapters is None:
            chapters = _chapters(remote, destination_id)
            _verify(engine, chapters, page_map_keyring)
        _pass(
            engine,
            remote,
            user_id,
            destination_id,
            clock,
            chapters,
            outcome,
            should_stop,
            max_session_hours * 3600,
            mode,
        )
    except _Stop as stop:
        outcome.code = stop.code
    log.info(
        "progress_sync.member_done", code=outcome.code, pulled=outcome.pulled,
        pushed=outcome.pushed, echoes=outcome.echoes, hidden=outcome.hidden,
    )  # fmt: skip
    return outcome


def sync_destination(
    engine: sa.Engine,
    remote: ProgressRemote,
    *,
    destination_id: str,
    clock: Any,
    should_stop: Callable[[], bool] | None = None,
    page_map_keyring: Callable[[], Keyring] | None = None,
    max_session_hours: float = DEFAULT_MAX_SESSION_HOURS,
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
    _verify(engine, chapters, page_map_keyring)
    results: dict[str, MemberOutcome] = {}
    down = False
    for user in members:
        if should_stop is not None and should_stop():
            break  # a pause or a maintenance window: the members left wait for the next run
        if down:
            results[user] = MemberOutcome("unavailable")
            continue
        results[user] = sync_member(
            engine, remote, user_id=user, destination_id=destination_id, clock=clock,
            chapters=chapters, should_stop=should_stop, page_map_keyring=page_map_keyring,
            max_session_hours=max_session_hours,
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


def _verify(
    engine: sa.Engine, chapters: Sequence[ChapterRef], keyring: Callable[[], Keyring] | None
) -> None:
    """The page counts of Kavita against the page maps of the blocks, once per pass and before any
    member's: what is not verified is not relayed. No keyring (or a locked one): nothing is."""
    if keyring is None:
        return
    try:
        key = store.fingerprint_key(keyring())
    except RuntimeError:  # a keyring that cannot be read: nothing is verified, nothing relayed
        log.info("progress_sync.page_map_skipped")
        return
    verify_page_maps(engine, chapters, key)


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


NOTE_BATCH: Final = 200  # chapters whose notes are written in one transaction


def _account_ref(engine: sa.Engine, account_id: str) -> str:
    """Which Kavita account and which key a note is made for. The row of an account survives a new
    link (another account, another key): the note must not."""
    account = identity.destination_account
    with engine.connect() as connection:
        row = connection.execute(
            sa.select(account.c.remote_user, account.c.token_fingerprint).where(
                account.c.id == account_id
            )
        ).first()
    if row is None:
        return ""
    return f"{str(row.remote_user).casefold()}|{row.token_fingerprint or ''}"


def _pass(
    engine: sa.Engine,
    remote: ProgressRemote,
    user_id: str,
    destination_id: str,
    clock: Any,
    chapters: Sequence[ChapterRef],
    outcome: MemberOutcome,
    should_stop: Callable[[], bool] | None = None,
    max_session_s: float = DEFAULT_MAX_SESSION_HOURS * 3600,
    mode: str = "sync",
) -> None:
    started = clock.now()
    account_id = _live(engine, user_id, destination_id)
    declared = _declared_name(engine, account_id)
    if _shared(engine, user_id, destination_id, declared):
        raise _Stop("account_shared")
    member = _open(engine, remote, user_id, destination_id)  # the key is opened once per pass
    try:
        _pass_open(
            engine, member, user_id, destination_id, account_id, declared, clock, started,
            chapters, outcome, should_stop, max_session_s, mode,
        )  # fmt: skip
    finally:
        member.close()


def _pass_open(
    engine: sa.Engine,
    member: MemberRemote,
    user_id: str,
    destination_id: str,
    account_id: str,
    declared: str,
    clock: Any,
    started: datetime,
    chapters: Sequence[ChapterRef],
    outcome: MemberOutcome,
    should_stop: Callable[[], bool] | None,
    max_session_s: float,
    mode: str = "sync",
) -> None:
    try:
        who = member.account_name()
    except TokenRefused:
        raise _Stop("token_refused") from None
    except (RemoteUnavailable, ChapterRejected):
        raise _Stop("unavailable") from None
    if not same_kavita_username(who, declared):
        raise _Stop("account_mismatch")  # the key opens another account than the one on file
    who_for = _account_ref(engine, account_id)
    notes = _load_notes(engine, user_id, destination_id, who_for)
    visible: list[ChapterRef] = []
    for ref in chapters:
        if _visible(engine, user_id, ref):
            visible.append(ref)
        else:
            outcome.hidden += 1
    restore = mode == "restore"
    # a restore has something to say about every chapter: the readings of Kavita are gone
    pending = (
        {r.chapter_id for r in visible}
        if restore
        else _pending_chapters(engine, user_id, visible, notes)
    )
    stamps = {} if mode != "sync" else _stamps(member, visible)
    writer = _NoteWriter(engine, user_id, destination_id, account_id, who_for)
    stopped = False
    try:
        for series in _by_series(visible):
            if should_stop is not None and should_stop():
                stopped = True  # a pause or a maintenance window: what is done stays done
                break
            stamp = stamps.get(series[0].series_id)
            if stamp is not None and all(
                (n := notes.get(r.chapter_id)) is not None
                and n.series_stamp == stamp
                and n.carrier_id == r.carrier_id
                and r.chapter_id not in pending
                for r in series
            ):
                continue  # nothing moved in Kavita, nothing new here: no call, no write
            for ref in series:
                _live(engine, user_id, destination_id)
                if not _visible(engine, user_id, ref):
                    outcome.hidden += 1  # the ceiling was lowered during the pass: nothing more
                    continue
                pushed_before = outcome.pushed
                try:
                    changed = _chapter(
                        engine, member, user_id, destination_id, ref, clock, started, outcome,
                        notes.get(ref.chapter_id), ref.chapter_id in pending, stamp,
                        max_session_s, mode,
                    )  # fmt: skip
                except ChapterRejected:
                    outcome.rejected += 1  # this chapter only: not for the account, or gone
                    continue
                except TokenRefused:
                    raise _Stop("token_refused") from None
                except RemoteUnavailable:
                    raise _Stop("unavailable") from None
                except MemberKeyRevokedError:
                    raise _Stop("member_key_revoked") from None
                if changed is not None:
                    writer.add(changed)
                    if outcome.pushed > pushed_before:
                        writer.flush()  # an echo must never be lost to a crash
    finally:
        writer.flush(cursor_at=None if stopped else started, pushed=outcome.pushed > 0)


def _by_series(chapters: Sequence[ChapterRef]) -> list[list[ChapterRef]]:
    groups: dict[int, list[ChapterRef]] = {}
    for ref in chapters:
        groups.setdefault(ref.series_id, []).append(ref)
    return list(groups.values())


def _stamps(member: MemberRemote, chapters: Sequence[ChapterRef]) -> dict[int, str]:
    """What the member's own series list says of each series: the way to know which series moved
    without asking about every chapter. Whatever goes wrong, every chapter is asked instead
    (a refused key is still seen by the first call that follows)."""
    if not chapters:
        return {}
    try:
        return member.series_stamps(sorted({r.library_id for r in chapters}))
    except (RemoteUnavailable, ChapterRejected):
        return {}
    except TokenRefused:
        raise _Stop("token_refused") from None


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
    chapter_id: int
    carrier_id: str
    series_id: int
    series_stamp: str | None
    pushed_page: int | None
    pushed_at: datetime | None
    seen_page: int | None
    seen_modified: str | None
    synced_at: datetime


def _utc(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


def _load_notes(
    engine: sa.Engine, user_id: str, destination_id: str, account_ref: str
) -> dict[int, _Note]:
    """The member's notes made for the Kavita account and key linked now: the notes of another
    account (or another key) are not read, and are replaced as the chapters are visited."""
    note = reading.progress_sync_note
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(note).where(
                note.c.shelf_destination_id == destination_id,
                note.c.user_id == user_id,
                note.c.account_ref == account_ref,
            )
        ).all()
    return {
        r.kavita_chapter_id: _Note(
            r.kavita_chapter_id, r.carrier_id, r.kavita_series_id or 0, r.series_stamp,
            r.pushed_page, _utc(r.pushed_at), r.seen_page, r.seen_modified,
            _utc(r.synced_at) or EPOCH,
        )
        for r in rows
    }  # fmt: skip


class _NoteWriter:
    """Notes that changed, written together (with the cursor of the pass) in one transaction: a
    pass that finds nothing new writes at most the cursor."""

    def __init__(
        self,
        engine: sa.Engine,
        user_id: str,
        destination_id: str,
        account_id: str,
        account_ref: str,
    ) -> None:
        self._engine = engine
        self._user = user_id
        self._destination = destination_id
        self._account = account_id
        self._ref = account_ref
        self._waiting: list[_Note] = []

    def add(self, note: _Note) -> None:
        self._waiting.append(note)
        if len(self._waiting) >= NOTE_BATCH:
            self.flush()

    def flush(self, *, cursor_at: datetime | None = None, pushed: bool = False) -> None:
        if not self._waiting and cursor_at is None:
            return
        keys = ("shelf_destination_id", "user_id", "kavita_chapter_id")
        with write_transaction(self._engine) as connection:
            for n in self._waiting:
                values: dict[str, Any] = {
                    "shelf_destination_id": self._destination, "user_id": self._user,
                    "kavita_chapter_id": n.chapter_id, "account_id": self._account,
                    "account_ref": self._ref, "carrier_id": n.carrier_id,
                    "kavita_series_id": n.series_id, "series_stamp": n.series_stamp,
                    "pushed_page": n.pushed_page, "pushed_at": n.pushed_at,
                    "seen_page": n.seen_page, "seen_modified": n.seen_modified,
                    "synced_at": n.synced_at,
                }  # fmt: skip
                connection.execute(
                    sqlite_insert(reading.progress_sync_note)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=list(keys),
                        set_={k: v for k, v in values.items() if k not in keys},
                    )
                )
            if cursor_at is not None:
                _upsert_cursor(connection, self._user, self._destination, cursor_at, pushed)
        self._waiting = []


def _not_transposed(position: sa.Table) -> sa.ColumnElement[bool]:
    """A position carried over from another Edition (EF-BIB-026) is a guess: never a page to push,
    never a mark of reading to relay."""
    return sa.func.json_extract(position.c.locator, "$.transposed").is_(None)


def _pending_chapters(
    engine: sa.Engine, user_id: str, refs: Sequence[ChapterRef], notes: dict[int, _Note]
) -> set[int]:
    """The chapters with a device position newer than their note, or a Unit a traversal marked
    read since (one read for each, for all of them): only these can have something to say to
    Kavita. A chapter a read through the block crossed has no position of its own."""
    if not refs:
        return set()
    since = {r.chapter_id: (notes[r.chapter_id].synced_at if r.chapter_id in notes else EPOCH)
             for r in refs}  # fmt: skip
    for r in refs:
        if r.chapter_id in notes and notes[r.chapter_id].carrier_id != r.carrier_id:
            since[r.chapter_id] = EPOCH  # another file under the same id: nothing is known of it
    position = reading.reading_position
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(position.c.carrier_id, sa.func.max(position.c.server_ts).label("last"))
            .where(
                position.c.user_id == user_id,
                position.c.carrier_type == "export_item",
                position.c.source == "device",
                _not_transposed(position),
                position.c.server_ts >= min(since.values()),
            )
            .group_by(position.c.carrier_id)
        ).all()
        marks = connection.execute(
            sa.select(position.c.server_ts, position.c.read_units).where(
                position.c.user_id == user_id,
                position.c.source == "device",
                _not_transposed(position),
                position.c.server_ts >= min(since.values()),
                sa.func.json_array_length(position.c.read_units) > 0,
            )
        ).all()
        units = _units_of(connection, {r.carrier_id for r in refs}) if marks else {}
    last = {str(r.carrier_id): _utc(r.last) for r in rows}
    found = {
        r.chapter_id
        for r in refs
        if (at := last.get(r.carrier_id)) is not None and at >= since[r.chapter_id]
    }
    for r in refs:
        covered = units.get(r.carrier_id, set())
        if (
            r.chapter_id not in found
            and covered
            and any(
                (when := _utc(m.server_ts)) is not None
                and when >= since[r.chapter_id]
                and covered & set(m.read_units)
                for m in marks
            )
        ):
            found.add(r.chapter_id)
    return found


def _units_of(connection: sa.Connection, carriers: set[str]) -> dict[str, set[str]]:
    cover = export.unit_coverage
    found: dict[str, set[str]] = {}
    for row in connection.execute(
        sa.select(cover.c.carrier_id, cover.c.unit_id).where(
            cover.c.carrier_type == "export_item", cover.c.carrier_id.in_(sorted(carriers))
        )
    ):
        found.setdefault(str(row.carrier_id), set()).add(str(row.unit_id))
    return found


def _chapter(
    engine: sa.Engine,
    member: MemberRemote,
    user_id: str,
    destination_id: str,
    ref: ChapterRef,
    clock: Any,
    started: datetime,
    outcome: MemberOutcome,
    old: _Note | None,
    has_pending: bool,
    stamp: str | None,
    max_session_s: float = DEFAULT_MAX_SESSION_HOURS * 3600,
    mode: str = "sync",
) -> _Note | None:
    """Pull, then push, one chapter. The note to write, or `None` when it would be the same.

    `mode` `"pull"`: the reading half only, and the note keeps its date (the positions still to be
    pushed are still to be pushed). `"restore"`: the page to push is the member's furthest proven
    one, whatever its source (a rescan of Kavita has deleted what it held)."""
    if old is not None and old.carrier_id != ref.carrier_id:
        old = None  # another file under the same id: its echo and its date are not this one's
    seen = member.get(ref)
    pushed_page = old.pushed_page if old else None
    pushed_at = old.pushed_at if old else None
    echo = pushed_page is not None and seen.page == pushed_page
    unchanged = (
        old is not None and old.seen_page == seen.page and old.seen_modified == seen.modified
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
    wanted: int | None = None
    if mode == "restore":
        wanted = _carried_page(engine, user_id, ref, outcome, max_session_s)
    elif mode == "sync" and (has_pending or old is None):
        refused = outcome.unverified
        wanted = _pending_page(
            engine, user_id, ref, old.synced_at if old else EPOCH, outcome, max_session_s
        )
        # a chapter Kavita holds under an id never seen (a rescan that renumbered it, or a first
        # look), not already refused above: what the member has proven is carried
        if wanted is None and old is None and outcome.unverified == refused:
            wanted = _carried_page(engine, user_id, ref, outcome, max_session_s)
    if wanted is not None and wanted > seen.page:
        _live(engine, user_id, destination_id)
        latest = member.get(ref)  # read again just before writing: never back from what it holds
        if wanted > latest.page:
            member.save(ref, wanted)
            outcome.pushed += 1
            pushed_page, pushed_at = wanted, clock.now()
            seen = RemotePosition(wanted, None)
    touched = (has_pending or old is None) and mode != "pull"
    new = _Note(
        ref.chapter_id, ref.carrier_id, ref.series_id, stamp, pushed_page, pushed_at,
        seen.page, seen.modified,
        started if touched else old.synced_at if old else EPOCH,
    )  # fmt: skip
    return None if new == old else new


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


def _pending_page(
    engine: sa.Engine,
    user_id: str,
    ref: ChapterRef,
    since: datetime,
    outcome: MemberOutcome,
    max_session_s: float = DEFAULT_MAX_SESSION_HOURS * 3600,
) -> int | None:
    """What to say to Kavita: the page of the member's **latest** device position since the last
    pass (the server's clock, `id` for a tie), on the content the file carries now; or, when a
    traversal marked every Unit of the file read since, « read » (the chapter a read through the
    block crossed has no position of its own).

    What came through a block (`locator.bundle`) is handed over only if the chapter's member of
    that block is `verified` against Kavita's own page count (`page_map`), else nothing is, and the
    chapter is counted (`unverified`). A position transposed from a replaced chapter is a guess
    and is never written."""
    position = reading.reading_position
    with engine.connect() as connection:
        current = _current_hash(connection, "export_item", ref.carrier_id)
        if current is None:
            return None
        row = connection.execute(
            sa.select(position.c.content_hash, position.c.locator, position.c.percent)
            .where(
                position.c.user_id == user_id,
                position.c.carrier_type == "export_item",
                position.c.carrier_id == ref.carrier_id,
                position.c.source == "device",
                _not_transposed(position),
                position.c.server_ts >= since,
            )
            .order_by(position.c.server_ts.desc(), position.c.id.desc())
            .limit(1)
        ).first()
        page: int | None = None
        speaking: list[dict[str, Any]] = []
        if row is not None and row.content_hash == current:
            locator = row.locator if isinstance(row.locator, dict) else {}
            if not locator.get("transposed"):
                found = locator.get("page")
                if isinstance(found, int) and not isinstance(found, bool):
                    page = found
                elif row.percent is not None and ref.pages > 0:
                    page = int(row.percent * ref.pages)
                if page is not None:
                    speaking.append(locator)
        finished = _all_read(connection, user_id, ref.carrier_id)
        marks = (
            _marking_locators(connection, user_id, ref.carrier_id, since, max_session_s)
            if finished
            else []
        )
        if page is None and not marks:
            return None
        for locator in [*speaking, *marks]:
            version = locator.get("bundle")
            if version is not None and not relayable(connection, version, ref.carrier_id):
                outcome.unverified += 1
                log.info("progress_sync.chapter_not_relayed", code="page_map_unverified")
                return None
    if marks and ref.pages > 0:
        return ref.pages
    return None if page is None else push_page(page, ref.pages, finished=finished)


def _carried_page(
    engine: sa.Engine,
    user_id: str,
    ref: ChapterRef,
    outcome: MemberOutcome,
    max_session_s: float = DEFAULT_MAX_SESSION_HOURS * 3600,
) -> int | None:
    """What a chapter that Kavita holds under an identifier it has just made is told: the page of
    the member's **furthest proven** position on this file (`reading_progress_proven`: a position
    carried over from another Edition is a guess and never counts), whatever its source, Kavita's
    own state pulled earlier included.

    Never a reading made up: « read » (the page count) only if every Unit of the file is read in
    the proven view **and** a traversal of a recent contact marked one (`_marking_locators`) or
    Kavita itself said so (a `server` position at the last page); else the page, capped as a push
    caps it. What came through a block is handed over only if its page map is verified."""
    position, view, cover = (
        reading.reading_position,
        _PROVEN,
        export.unit_coverage,
    )
    with engine.connect() as connection:
        current = _current_hash(connection, "export_item", ref.carrier_id)
        if current is None:
            return None
        rows = connection.execute(
            sa.select(position.c.locator, position.c.percent, position.c.source)
            .join(view, view.c.last_position_id == position.c.id)
            .join(cover, sa.and_(cover.c.unit_id == view.c.unit_id,
                                 cover.c.carrier_type == "export_item",
                                 cover.c.carrier_id == ref.carrier_id))
            .where(
                view.c.user_id == user_id,
                position.c.carrier_type == "export_item",
                position.c.carrier_id == ref.carrier_id,
                position.c.content_hash == current,
                _not_transposed(position),
            )
        ).all()  # fmt: skip
        best: int | None = None
        speaking: list[dict[str, Any]] = []
        server_read = False
        for row in rows:
            locator = row.locator if isinstance(row.locator, dict) else {}
            found = locator.get("page")
            if isinstance(found, int) and not isinstance(found, bool):
                page: int | None = found
            elif row.percent is not None and ref.pages > 0:
                page = int(row.percent * ref.pages)
            else:
                page = None
            if page is None:
                continue
            if row.source == "server" and ref.pages > 0 and page >= ref.pages:
                server_read = True
            if best is None or page > best:
                best = page
            speaking.append(locator)
        finished = _all_read(connection, user_id, ref.carrier_id)
        marks = (
            _marking_locators(connection, user_id, ref.carrier_id, EPOCH, max_session_s)
            if finished and not server_read
            else []
        )
        if best is None and not marks:
            return None
        for locator in [*speaking, *marks]:
            version = locator.get("bundle")
            if version is not None and not relayable(connection, version, ref.carrier_id):
                outcome.unverified += 1
                log.info("progress_sync.chapter_not_relayed", code="page_map_unverified")
                return None
    if finished and (marks or server_read) and ref.pages > 0:
        return ref.pages
    return None if best is None else push_page(best, ref.pages, finished=False)


def _marking_locators(
    connection: sa.Connection,
    user_id: str,
    carrier_id: str,
    since: datetime,
    max_session_s: float = DEFAULT_MAX_SESSION_HOURS * 3600,
) -> list[dict[str, Any]]:
    """The locators of the positions, since `since`, whose traversal marked a Unit of this file
    read (the position that closed the move may be on another file of the block)."""
    units = _units_of(connection, {carrier_id}).get(carrier_id, set())
    position = reading.reading_position
    found: list[dict[str, Any]] = []
    for row in connection.execute(
        sa.select(position.c.locator, position.c.read_units).where(
            position.c.user_id == user_id,
            position.c.source == "device",
            _not_transposed(position),
            position.c.server_ts >= since,
            sa.func.json_array_length(position.c.read_units) > 0,
        )
    ):
        if units & set(row.read_units):
            locator = row.locator if isinstance(row.locator, dict) else {}
            gap = locator.get("gap")
            if isinstance(gap, int | float) and not isinstance(gap, bool) and gap > max_session_s:
                log.info("progress_sync.chapter_not_relayed", code="session_too_long")
                continue  # the move did not start from a recent contact: it stays local
            found.append(locator)
    return found


# the proven view: a mark carried over from another Edition is not a reading
_PROVEN: Final = sa.table(
    "reading_progress_proven",
    sa.column("user_id"),
    sa.column("unit_id"),
    sa.column("state"),
    sa.column("last_position_id"),
)


def _all_read(connection: sa.Connection, user_id: str, carrier_id: str) -> bool:
    cover, view = export.unit_coverage, _PROVEN
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


def _upsert_cursor(
    connection: sa.Connection, user_id: str, destination_id: str, started: datetime, pushed: bool
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
    connection.execute(statement)
