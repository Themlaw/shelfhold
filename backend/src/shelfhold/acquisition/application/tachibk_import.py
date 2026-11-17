"""Importing a Mihon backup (`.tachibk`): preview, then apply (EF-INT-008, ENF-SEC-010, D14).

The file is read as hostile, in a bounded subprocess (`platform.import_tachibk`). The import is two
steps, both the admin's (a fresh admin session, or the host's command line: never a key, a member
or a child):

* **preview** reads the file and keeps what would be written (`tachibk_import.staged`). The library
  sees nothing of it: no pending item, no marker, no hint;
* **apply** writes it, once, in one transaction: a series of the backup is a **pending source item**
  (`mihon:<source id>` and the address the backup gave, kept as text and never called: the source
  is resolved later, through the Fetcher and its guard); the chapters it lists are known units, in
  the item's memo; what the file proves read is a **read marker** of the profile the admin chose
  (and of nobody else); a tracker entry is a **hint**, with no secret.

What it never does: mark a chapter read beyond what the file proves (the marks a profile earns
while reading are `reading_position`'s, and this touches none of them), keep the name of a source,
write to a profile that is not the chosen one, or open a path or an address of the file.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.identity.public import AuditEntry, Credential, Principal, audit_insert
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, identity
from shelfhold.platform.import_tachibk.decode import TachibkRefusedError
from shelfhold.platform.import_tachibk.map import Staged, map_backup
from shelfhold.platform.import_tachibk.sandbox import decode_in_sandbox

__all__ = [
    "ApplyDone",
    "ImportItem",
    "ImportView",
    "TachibkImportError",
    "TachibkImportService",
    "TachibkRefusedError",
]

log = structlog.get_logger(__name__)

NOT_FOUND = "import.tachibk_not_found"
APPLIED = "import.tachibk_applied"
ORIGIN = "tachibk"
SHOWN_ITEMS = 100  # the series a preview lists: the counts are those of the whole file

_import = acquisition.tachibk_import
_pending = acquisition.pending_source_item
_marker = acquisition.read_marker
_hint = acquisition.tracker_hint


class TachibkImportError(Exception):
    """A refusal that is not the file's: the batch is absent, or already applied."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ImportItem:
    title: str
    source_key: str
    chapters: int
    read: int
    started: int
    trackers: int


@dataclass(frozen=True)
class ImportView:
    id: str
    profile_id: str
    schema_version: str
    state: str
    series: int
    chapters: int
    read: int
    started: int
    trackers: int
    sources: int
    skipped: dict[str, int]
    items: list[ImportItem] = field(default_factory=list)


@dataclass(frozen=True)
class ApplyDone:
    id: str
    state: str
    pending_items: int
    markers: int
    hints: int


def _summary(staged: Staged) -> dict[str, Any]:
    return {
        "series": len(staged.series), "chapters": staged.chapters, "read": staged.read,
        "started": staged.started, "trackers": staged.trackers, "sources": staged.sources,
        "skipped": {
            "not_favorite": staged.skipped_not_favorite, "local": staged.skipped_local,
            "invalid": staged.skipped_invalid, "trackers": staged.skipped_trackers,
        },
        "items": [
            {
                "title": s.title, "source_key": s.source_key, "chapters": len(s.units),
                "read": sum(1 for m in s.markers if m.read),
                "started": sum(1 for m in s.markers if not m.read), "trackers": len(s.hints),
            }
            for s in staged.series[:SHOWN_ITEMS]
        ],
    }  # fmt: skip


def _view(row: Any) -> ImportView:
    data = dict(row.summary or {})
    return ImportView(
        id=row.id, profile_id=row.user_id, schema_version=row.schema_version, state=row.state,
        series=int(data.get("series", 0)), chapters=int(data.get("chapters", 0)),
        read=int(data.get("read", 0)), started=int(data.get("started", 0)),
        trackers=int(data.get("trackers", 0)), sources=int(data.get("sources", 0)),
        skipped={str(k): int(v) for k, v in dict(data.get("skipped", {})).items()},
        items=[ImportItem(**i) for i in data.get("items", [])],
    )  # fmt: skip


def _admin(principal: Principal) -> None:
    """Only the admin's own session or the host's command line: never a key, a token, a member."""
    if principal.is_host:
        return
    if principal.credential is not Credential.SESSION or principal.role != "admin":
        raise TachibkImportError("forbidden")


class TachibkImportService:
    def __init__(self, engine: Engine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    # -- preview ----------------------------------------------------------------------------------

    def preview(
        self, principal: Principal, data: bytes, *, profile_id: str | None = None
    ) -> ImportView:
        """Read the file (bounded, in a subprocess), keep what it would write, write nothing else.
        `TachibkRefusedError` for the file; `TachibkImportError` for an absent profile."""
        _admin(principal)
        subject = self._subject(principal, profile_id)
        with self._engine.connect() as connection:  # the profile exists before a byte is read
            if not self._profile_exists(connection, subject):
                raise TachibkImportError(NOT_FOUND)
        staged = map_backup(decode_in_sandbox(data))
        now = self._clock.now()
        import_id = new_ulid()
        with write_transaction(self._engine) as connection:
            if not self._profile_exists(connection, subject):
                raise TachibkImportError(NOT_FOUND)
            connection.execute(
                sa.insert(_import).values(
                    id=import_id, user_id=subject, created_by=_account(principal),
                    schema_version=staged.schema_version,
                    content_hash=hashlib.sha256(data).hexdigest(), state="previewed",
                    staged=staged.to_json(), summary=_summary(staged), created_at=now,
                )
            )  # fmt: skip
            connection.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=principal.actor_id,
                        action="import.tachibk_previewed",
                        at=now,
                        target=f"tachibk_import:{import_id}",
                        session_id=principal.session_id,
                        auth_time=principal.auth_time,
                    )
                )
            )
            row = connection.execute(sa.select(_import).where(_import.c.id == import_id)).one()
        log.info("tachibk_previewed", import_id=import_id, series=len(staged.series))
        return _view(row)

    def _subject(self, principal: Principal, profile_id: str | None) -> str:
        if profile_id is not None:
            return profile_id
        with self._engine.connect() as connection:
            if principal.is_host:  # the host has no account: its profile is the first admin's
                found = connection.execute(
                    sa.select(identity.user.c.id)
                    .where(identity.user.c.role == "admin")
                    .order_by(identity.user.c.created_at, identity.user.c.id)
                ).scalar()
                if found is None:
                    raise TachibkImportError(NOT_FOUND)
                return str(found)
        return principal.user_id

    @staticmethod
    def _profile_exists(connection: Connection, user_id: str) -> bool:
        return (
            connection.execute(
                sa.select(identity.user.c.id).where(identity.user.c.id == user_id)
            ).first()
            is not None
        )

    def profile_id(self, username: str) -> str | None:
        """The profile named `username`, for the command line (which names, never an id)."""
        with self._engine.connect() as connection:
            found = connection.execute(
                sa.select(identity.user.c.id).where(identity.user.c.username == username)
            ).scalar()
        return None if found is None else str(found)

    # -- reading ----------------------------------------------------------------------------------

    def review(self, principal: Principal, import_id: str) -> ImportView:
        _admin(principal)
        with self._engine.connect() as connection:
            row = connection.execute(sa.select(_import).where(_import.c.id == import_id)).first()
        if row is None:
            raise TachibkImportError(NOT_FOUND)
        return _view(row)

    # -- applying ---------------------------------------------------------------------------------

    def apply(self, principal: Principal, import_id: str) -> ApplyDone:
        """Write what the preview kept, once. The profile is the one chosen at the preview."""
        _admin(principal)
        now = self._clock.now()
        with write_transaction(self._engine) as connection:
            row = connection.execute(sa.select(_import).where(_import.c.id == import_id)).first()
            if row is None:
                raise TachibkImportError(NOT_FOUND)
            if row.state != "previewed" or row.staged is None:
                raise TachibkImportError(APPLIED)
            staged = Staged.from_json(row.staged)
            items = markers = hints = 0
            for series in staged.series:
                item_id = _upsert_item(connection, series, now)
                items += 1
                markers += _upsert_markers(connection, row.user_id, item_id, import_id, series)
                hints += _upsert_hints(connection, row.user_id, item_id, import_id, series)
            connection.execute(
                sa.update(_import).where(_import.c.id == import_id).values(
                    state="applied", staged=None, applied_at=now,
                )
            )  # fmt: skip
            connection.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=principal.actor_id,
                        action="import.tachibk_applied",
                        at=now,
                        target=f"tachibk_import:{import_id}",
                        session_id=principal.session_id,
                        auth_time=principal.auth_time,
                    )
                )
            )
        log.info("tachibk_applied", import_id=import_id, series=items, markers=markers)
        return ApplyDone(import_id, "applied", items, markers, hints)


def _account(principal: Principal) -> str | None:
    return principal.user_id if principal.credential is Credential.SESSION else None


def _memo(series: Any) -> dict[str, Any]:
    return {
        "origin": ORIGIN,
        "units": [{"ref": u.ref, "number": u.number, "state": u.state} for u in series.units],
    }


def _upsert_item(connection: Connection, series: Any, now: Any) -> str:
    """The pending item of a series. One that a search made keeps what it knew; one an earlier
    backup made takes the newer title and chapters."""
    existing = connection.execute(
        sa.select(_pending.c.id, _pending.c.memo).where(
            _pending.c.source_key == series.source_key, _pending.c.series_ref == series.series_ref
        )
    ).first()
    if existing is None:
        item_id = new_ulid()
        connection.execute(
            sa.insert(_pending).values(
                id=item_id, source_key=series.source_key, series_ref=series.series_ref,
                title=series.title, fetched_at=now, memo=_memo(series),
            )
        )  # fmt: skip
        return item_id
    memo = existing.memo
    if isinstance(memo, str):
        memo = json.loads(memo)
    if isinstance(memo, dict) and memo.get("origin") == ORIGIN:
        connection.execute(
            sa.update(_pending).where(_pending.c.id == existing.id).values(
                title=series.title, fetched_at=now, memo=_memo(series)
            )
        )  # fmt: skip
    return str(existing.id)


def _upsert_markers(
    connection: Connection, user_id: str, item_id: str, import_id: str, series: Any
) -> int:
    """A marker only ever goes forward: read stays read, a page keeps the furthest."""
    if not series.markers:
        return 0
    stmt = sqlite_insert(_marker)
    last = stmt.excluded.last_page
    connection.execute(
        stmt.on_conflict_do_update(
            index_elements=["user_id", "pending_item_id", "chapter_ref"],
            set_={
                "read": sa.func.max(_marker.c.read, stmt.excluded.read),
                "last_page": sa.case(
                    (last.is_(None), _marker.c.last_page),
                    (_marker.c.last_page.is_(None), last),
                    else_=sa.func.max(_marker.c.last_page, last),
                ),
                "import_id": stmt.excluded.import_id,
            },
        ),
        [
            {
                "id": new_ulid(), "user_id": user_id, "pending_item_id": item_id,
                "import_id": import_id, "chapter_ref": m.chapter_ref, "number": m.number,
                "read": m.read, "last_page": m.last_page,
            }
            for m in series.markers
        ],
    )  # fmt: skip
    return len(series.markers)


def _upsert_hints(
    connection: Connection, user_id: str, item_id: str, import_id: str, series: Any
) -> int:
    if not series.hints:
        return 0
    stmt = sqlite_insert(_hint)
    connection.execute(
        stmt.on_conflict_do_update(
            index_elements=["user_id", "pending_item_id", "tracker"],
            set_={
                "remote_id": stmt.excluded.remote_id,
                "last_chapter": stmt.excluded.last_chapter,
                "import_id": stmt.excluded.import_id,
            },
        ),
        [
            {
                "id": new_ulid(), "user_id": user_id, "pending_item_id": item_id,
                "import_id": import_id, "tracker": h.tracker, "remote_id": h.remote_id,
                "last_chapter": h.last_chapter,
            }
            for h in series.hints
        ],
    )  # fmt: skip
    return len(series.hints)
