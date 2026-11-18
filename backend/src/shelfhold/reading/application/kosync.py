"""The kosync service: what KOReader writes and reads, translated to the places of the Work
(M4 L47, ADR-0053 §5 and §6, EF-LEC-009, EF-LEC-014, EF-USR-006).

KOReader names a file by the partial MD5 of its bytes (`document`). That digest is resolved, for the
profile of the device token and nobody else, to one of two things and refused for anything else:

* an exported file (`document_alias.koreader_partial_md5`): its position is stored on that file;
* a block of a series (`bundle_version.koreader_md5`, the profile's own version): the page of the
  block is translated by `bundle_member` into a chapter and a page of that chapter, and stored on
  the chapter's own file. A position is never anchored on the block.

**An unknown digest, one the profile may not see (above her ceiling, another profile's block,
purged) and one that is not a digest are the same answer**, and they cost the same: the same
statements run, in the same order, whether anything was found or not (`resolve`). Nothing is stored.
A digest held by two files the profile may see is `AmbiguousDocumentError` (a guess would write on
the wrong file); a file she may not see never makes it one.

**What a read gives back** is the furthest position (`reading_progress.last_position_id`,
`docs/reference/reading-progress.md`), with the date of that very record (never "now", and never in
the future), in the frame of the document asked: a position kept on the file itself as it is; on a
chapter of the block, as the page of the block; the other way round, the page of the chapter's file.
A position that cannot be translated (another file of the same Unit, a chapter replaced since the
version, a purged chapter) is not given: the furthest decides, and an older one never stands in for
it, since it would move the reader backwards.

**The page** arrives as text (`tostring(progress)`): it is a whole number from 1, else
`InvalidProgressError` (a 422). An EPUB (reflowable) sends an xpointer instead; it is stored as it
is, in `locator.xpointer`, and marks nothing read (`traverse=False`) until L51. The device name is
kept in the locator (`device`); the date of a position is always the server's.
"""

import hashlib
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import visible_edition_ids
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import bundle, export, reading
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.reading.application.progress import (
    HISTORY_LIMIT,
    UnknownCarrierError,
    _current_hash,
    mark_traversed_in_frame,
    progress_of,
    record_position,
)
from shelfhold.reading.bundle import store
from shelfhold.reading.bundle.plan import LIVE
from shelfhold.reading.domain.traversal import Span, Step

log = structlog.get_logger(__name__)

__all__ = [
    "AmbiguousDocumentError",
    "InvalidProgressError",
    "Kosync",
    "Pulled",
    "UnknownCounter",
]

MAX_TEXT: Final = 128  # a device name, a device identifier
MAX_XPOINTER: Final = 2048
MAX_PAGE: Final = 10**9
_DIGEST = re.compile(r"[0-9a-fA-F]{32}")
_DIGITS = re.compile(r"[0-9]{1,10}")
_ABSENT: Final = "0" * 26  # a placeholder that is no Edition: a lookup always runs
_REFLOWABLE = frozenset({"epub"})
_DELIVERY = "delivery"  # the page 0 the server writes when it hands a file over
_UNKNOWN_BOUND = 1024  # tokens kept by the counter
_RETURNED_BOUND = 1024  # (profile, token) pairs whose last given page is kept
WINDOW_S: Final = 60  # one first and one last position per file, device and window (MIN-4)
GUARD_FLOOR: Final = 0.0001  # KOReader floors the share to the ten-thousandth


class InvalidProgressError(ValueError):
    """The page is not a whole number from 1 (or is outside the block), or a field is not valid
    (a 422)."""


class AmbiguousDocumentError(LookupError):
    """Two files the profile may see hold that digest (a 409): nothing is written or read."""


@dataclass(frozen=True)
class Pulled:
    """What the read gives back, in the words of the protocol."""

    document: str
    progress: str
    percentage: float
    device: str
    device_id: str | None
    timestamp: int

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "document": self.document,
            "progress": self.progress,
            "percentage": self.percentage,
            "device": self.device,
            "timestamp": self.timestamp,
        }
        if self.device_id is not None:
            out["device_id"] = self.device_id
        return out


class UnknownCounter:
    """How many positions each token sent that Shelfhold did not take, and why (unknown digest,
    guard of integrity, a chapter that is gone...). The device is always answered 200 (a refusal
    would jam its queue, ADR-0053 amendment of 2026-11-16): this counter and the log keep the real
    reason. In memory, bounded, and it keeps the token's public identifier only, never what was
    asked."""

    def __init__(self) -> None:
        self._counts: dict[str, dict[str, int]] = {}

    def hit(self, token: str, reason: str = "unknown") -> None:
        token = token.lower()
        if token not in self._counts and len(self._counts) >= _UNKNOWN_BOUND:
            del self._counts[next(iter(self._counts))]
        reasons = self._counts.setdefault(token, {})
        reasons[reason] = reasons.get(reason, 0) + 1

    def of(self, token: str, reason: str | None = None) -> int:
        reasons = self._counts.get(token.lower(), {})
        return sum(reasons.values()) if reason is None else reasons.get(reason, 0)

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {token: dict(reasons) for token, reasons in self._counts.items()}


@dataclass(frozen=True)
class _Member:
    rank: int
    item_id: str | None
    unit_id: str | None
    fp: str | None
    first_page: int
    page_count: int
    state: str = "candidate"  # `verified` once Kavita counted the same pages (M4 L50)


@dataclass(frozen=True)
class _Doc:
    """What a digest resolved to for the profile (`digest`: as the device wrote it)."""

    digest: str
    edition_id: str
    item_id: str | None = None  # an exported file...
    version: store.VersionRow | None = None  # ...or a block of a series
    version_pages: int = 0


def _clean(text: str, *, limit: int) -> str:
    """A name from a device: not empty, bounded, and without a control or format character."""
    if not 1 <= len(text) <= limit or any(
        unicodedata.category(c) in {"Cc", "Cf", "Cs", "Co", "Cn"} for c in text
    ):
        raise InvalidProgressError("text field")
    return text


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _who(device_id: str | None, token: str) -> str:
    """The device of a position: its identifier, else (a client that sends none) its token, that
    is kept only as a short digest."""
    if device_id is not None:
        return "d:" + device_id
    return "t:" + hashlib.sha256(token.lower().encode()).hexdigest()[:16]


def _digest_of(raw: str) -> str | None:
    return "md5:" + raw.lower() if _DIGEST.fullmatch(raw) else None


class Kosync:
    def __init__(
        self,
        engine: Engine,
        keyring: Callable[[], Keyring],
        now: Callable[[], datetime],
        unknown: UnknownCounter | None = None,
    ) -> None:
        self._engine = engine
        self._keyring = keyring
        self._now = now
        self.unknown = unknown or UnknownCounter()
        # The last page of a block a GET gave to each (profile, token): (version, page of the block,
        # when). In memory and bounded, on purpose: it is a hint about a start, not a fact to keep
        # (lost at a restart, the next PUT of a device is taken as a position like any other).
        self._returned: dict[tuple[str, str], tuple[str, int, datetime]] = {}

    # -- resolution -------------------------------------------------------------------------

    def _resolve(self, connection: Connection, user_id: str, raw: str) -> _Doc | None:
        """The one door. Whatever the digest is, the same four statements run: the alias lookup, the
        block lookup, the reader's scope and the visibility of the Editions found (a placeholder
        when none was)."""
        digest = _digest_of(raw) or "md5:" + "0" * 32
        echo = raw
        alias, item = export.document_alias, export.export_item
        items = connection.execute(
            sa.select(item.c.id, item.c.edition_id)
            .select_from(alias.join(item, item.c.id == alias.c.export_item_id))
            .where(alias.c.koreader_partial_md5 == digest, item.c.kind != "opds_bundle")
            .distinct()
        ).all()
        version = bundle.bundle_version
        blocks = connection.execute(
            sa.select(version.c.id, version.c.edition_id, version.c.page_count).where(
                version.c.koreader_md5 == digest,
                version.c.profile_user_id == user_id,
                version.c.purged_at.is_(None),
            )
        ).all()
        scope = reader_scope(connection, user_id)
        editions = {str(r.edition_id) for r in items} | {str(r.edition_id) for r in blocks}
        seen = visible_edition_ids(connection, scope, sorted(editions) or [_ABSENT])
        found: list[_Doc] = []
        if _digest_of(raw) is not None:
            found += [_Doc(echo, str(r.edition_id), item_id=str(r.id)) for r in items
                      if str(r.edition_id) in seen]  # fmt: skip
            for r in blocks:
                if str(r.edition_id) in seen:
                    row = store.by_id(connection, str(r.id))
                    if row is not None:
                        found.append(_Doc(echo, str(r.edition_id), version=row,
                                          version_pages=int(r.page_count)))  # fmt: skip
        if len(found) > 1:
            raise AmbiguousDocumentError
        return found[0] if found else None

    def _members(self, connection: Connection, version_id: str) -> list[_Member]:
        return [
            _Member(m.rank, m.item_id, m.unit_id, m.fp, m.first_page, m.page_count, m.state)
            for m in store.members_of(connection, version_id)
        ]

    def _anchor(self, connection: Connection, item_id: str) -> str | None:
        """The hash of the content the file carries now, the very one the view calls "not replaced"
        (`progress._current_hash`: the content, else the latest alias); a file the exporter has not
        linked to a content yet is anchored on its live file."""
        found = _current_hash(connection, "export_item", item_id)
        if found is None:
            found = connection.execute(
                sa.select(export.export_record.c.file_hash)
                .where(
                    export.export_record.c.export_item_id == item_id,
                    export.export_record.c.state.in_(LIVE),
                    export.export_record.c.file_hash.is_not(None),
                )
                .order_by(export.export_record.c.file_hash)
                .limit(1)
            ).scalar()
        return None if found is None else str(found)

    def _still_the_member(self, connection: Connection, member: _Member) -> bool:
        """The chapter is the one the version was made from (its file has the recorded
        fingerprint), else its pages are not the pages of the block."""
        if member.item_id is None or member.fp is None:
            return False
        key = store.fingerprint_key(self._keyring())
        hashes = connection.execute(
            sa.select(export.export_record.c.file_hash).where(
                export.export_record.c.export_item_id == member.item_id,
                export.export_record.c.state.in_(LIVE),
            )
        ).scalars()
        return any(h is not None and store.member_fp(key, str(h)) == member.fp for h in hashes)

    def _kind(self, connection: Connection, item_id: str) -> str | None:
        """`reflowable` (an EPUB), `paged`, or None when no record says: the first of the formats
        of the file, whatever the order the rows come in."""
        formats = {
            str(f).lower()
            for f in connection.execute(
                sa.select(export.export_record.c.format).where(
                    export.export_record.c.export_item_id == item_id,
                    export.export_record.c.format.is_not(None),
                )
            ).scalars()
        }
        if not formats:
            return None
        return "reflowable" if formats & _REFLOWABLE else "paged"

    # -- the write --------------------------------------------------------------------------

    def push(
        self,
        *,
        user_id: str,
        token: str,
        document: str,
        progress: str,
        percentage: float,
        device: str,
        device_id: str | None,
    ) -> bool:
        """Records where the device is. True: stored. False: accepted without effect, for a reason
        that goes to the log and the counter and never to the device (unknown or hidden digest, two
        files with one digest, a chapter that is gone, a page beyond the block, a percentage that
        is not of this file, a format nobody knows). Only a malformed field is an error."""
        device = _clean(device, limit=MAX_TEXT)
        device_id = None if device_id is None else _clean(device_id, limit=MAX_TEXT)
        if not isinstance(percentage, int | float) or not math.isfinite(percentage):
            raise InvalidProgressError("percentage")
        if not 0.0 <= percentage <= 1.0:
            raise InvalidProgressError("percentage")
        with write_transaction(self._engine) as connection:
            try:
                doc = self._resolve(connection, user_id, document)
            except AmbiguousDocumentError:
                return self._refused(token, "ambiguous")
            if doc is None:
                return self._refused(token, "unknown")
            now = self._now()
            bundled: tuple[store.VersionRow, int] | None = None
            if doc.version is not None:
                page = _page(progress)
                if page > doc.version_pages:
                    return self._refused(token, "beyond_block")
                target = self._into_chapter(connection, doc, doc.version, page, percentage, token)
                if target is None:
                    return False
                item_id, locator, percent = target
                transposed = locator.get("transposed") is True
                traverse = not transposed  # a transposed share is no proof of a reading
                bundled = None if transposed else (doc.version, page)
                if transposed:
                    log.info("kosync.transposed", token=token.lower())  # I30: stored, never relayed
                else:
                    locator["bundle"] = doc.version.id
                    locator["bundle_page"] = page
                    locator["who"] = _who(device_id, token)
                    if self._is_echo(connection, user_id, token, doc, doc.version, page):
                        locator["anchor"] = True  # a start (the page a GET gave), never a reading
            elif doc.item_id is not None:
                item_id = doc.item_id
                kind = self._kind(connection, item_id)
                if kind == "reflowable":
                    locator = {"xpointer": _xpointer(progress)}
                    traverse = False
                elif kind == "paged" or _DIGITS.fullmatch(progress.strip()):
                    locator = {"page": _page(progress)}
                    traverse = True
                else:
                    return self._refused(token, "unknown_format")
                percent = percentage
            else:
                return self._refused(token, "unknown")
            anchor = self._anchor(connection, item_id)
            if anchor is None:
                return self._refused(token, "no_anchor")
            locator["device"] = device
            replaces = self._coalesce(connection, user_id, item_id, device_id, now, locator)
            try:
                position_id = record_position(
                    connection, reader_id=user_id, subject_id=user_id, carrier_type="export_item",
                    carrier_id=item_id, content_hash=anchor, locator=locator, percent=percent,
                    client_ts=now, device_id=device_id, now=now, traverse=traverse,
                )  # fmt: skip
            except UnknownCarrierError:
                return self._refused(token, "hidden")
            if replaces is not None:
                connection.execute(
                    sa.delete(reading.reading_position).where(
                        reading.reading_position.c.id == replaces
                    )
                )
            if bundled is not None and locator.get("anchor") is not True:
                self._cross_chapters(connection, user_id, position_id, token, *bundled)
            return True

    def _refused(self, token: str, reason: str) -> bool:
        self.unknown.hit(token, reason)
        log.info("kosync.not_stored", token=token.lower(), reason=reason)
        return False

    def _coalesce(
        self,
        connection: Connection,
        user_id: str,
        item_id: str,
        device_id: str | None,
        now: datetime,
        locator: dict[str, Any],
    ) -> str | None:
        """At most two rows per (profile, file, device) and window of `WINDOW_S` seconds: the first
        (`f`), and the last, which each new position of the window replaces. The traversal needs
        the anchors of a move, and the speed of a move from the first to the last of a window is the
        speed it had: nothing it counts is lost. A row that carries a mark is never replaced.
        Writes the window into `locator` and returns the id of the row to delete."""
        table = reading.reading_position
        stamp = int(now.timestamp())
        last = connection.execute(
            sa.select(table.c.id, table.c.locator, table.c.read_units, table.c.undone_units)
            .where(
                table.c.user_id == user_id,
                table.c.carrier_type == "export_item",
                table.c.carrier_id == item_id,
                table.c.source == "device",
                table.c.device_id.is_(None)
                if device_id is None
                else table.c.device_id == device_id,
            )
            .order_by(table.c.server_ts.desc(), table.c.id.desc())
            .limit(1)
        ).first()
        start = None if last is None else last.locator.get("w")
        if (
            last is None
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not 0 <= stamp - start < WINDOW_S
        ):
            locator.update({"w": stamp, "f": True})
            return None
        locator["w"] = start
        if (
            last.locator.get("f") is True
            or last.locator.get("anchor") is True
            or last.read_units
            or last.undone_units != {}
        ):
            return None  # the first of the window, a start, or a row that carries a mark: kept
        return str(last.id)

    def _is_echo(
        self, connection: Connection, user_id: str, token: str, doc: _Doc,
        version: store.VersionRow, page: int,
    ) -> bool:  # fmt: skip
        """The page is the one a GET gave this token (or an earlier page of the same chapter): the
        device went there because it was told to, and sends it back. It is where the reader starts,
        not a place the reader got to."""
        entry = self._returned.get((user_id, token.lower()))
        if entry is not None and entry[0] == version.id:
            given = entry[1]
        else:
            # Nothing in memory (a restart since the GET, J-REP-3): the page a GET would give is
            # read again from the positions as they are, before this PUT is written. A device
            # that goes where it was told, and sends it back, is never a reader who skipped
            # the chapters in between. Where nothing would be given, it is no echo.
            found = self._furthest(connection, user_id, doc, None)
            if found is None or not found.progress.isdigit():
                return False
            given = int(found.progress)
        return any(
            m.first_page <= given < m.first_page + m.page_count and m.first_page <= page <= given
            for m in self._members(connection, version.id)
        )

    def _cross_chapters(
        self, connection: Connection, user_id: str, position_id: str, token: str,
        version: store.VersionRow, page: int,
    ) -> None:  # fmt: skip
        """The traversal in the frame of the block: the positions of **this device** on this
        version, as pages of the block, and every chapter a plausible move covered whole is read,
        wherever its file is (ADR-0053 §5: "la chaîne peut franchir plusieurs chapitres").

        The chain starts at the latest start: a position that echoes a page a GET gave (`anchor`),
        or that GET itself (the page it gave, and when). Another device's positions are never a
        step: a move is one device's. The delay between the last two steps is written on the
        position that closed the move (`gap`, in seconds): what the sync reads to relay « read »
        only to a move that started from a recent contact."""
        table = reading.reading_position
        who = self._who_of(connection, position_id)
        rows = connection.execute(
            sa.select(
                sa.func.json_extract(table.c.locator, "$.bundle_page").label("page"),
                sa.func.json_extract(table.c.locator, "$.anchor").label("anchor"),
                table.c.server_ts,
            )
            .where(
                table.c.user_id == user_id,
                table.c.source == "device",
                sa.func.json_extract(table.c.locator, "$.bundle") == version.id,
                sa.func.json_extract(table.c.locator, "$.who") == who,
            )
            .order_by(table.c.server_ts.desc(), table.c.id.desc())
            .limit(HISTORY_LIMIT)
        ).all()
        chain: list[Step] = []
        anchored: datetime | None = None
        for r in rows:  # newest first, up to the latest start
            if isinstance(r.page, int):
                chain.append(Step(float(r.page), _utc(r.server_ts)))
            if r.anchor:
                anchored = _utc(r.server_ts)
                break
        steps = list(reversed(chain))
        given = self._returned.get((user_id, token.lower()))
        if (
            given is not None
            and given[0] == version.id
            and (anchored is None or _utc(given[2]) >= anchored)
        ):
            at = _utc(given[2])
            steps = [Step(float(given[1]), at), *[s for s in steps if s.ts > at]]
        spans = [
            Span(m.unit_id, float(m.first_page), float(m.first_page + m.page_count - 1))
            for m in self._members(connection, version.id)
            if m.unit_id and m.item_id and m.page_count > 0
        ]
        if len(steps) >= 2 and spans:
            marked = mark_traversed_in_frame(connection, position_id, steps=steps, spans=spans)
            if marked:
                self._write_gap(connection, position_id, steps[-1].ts - steps[-2].ts)

    def _who_of(self, connection: Connection, position_id: str) -> str | None:
        found = connection.execute(
            sa.select(sa.func.json_extract(reading.reading_position.c.locator, "$.who")).where(
                reading.reading_position.c.id == position_id
            )
        ).scalar()
        return None if found is None else str(found)

    def _write_gap(self, connection: Connection, position_id: str, gap: Any) -> None:
        table = reading.reading_position
        locator = connection.execute(
            sa.select(table.c.locator).where(table.c.id == position_id)
        ).scalar_one()
        locator = dict(locator)
        locator["gap"] = max(0, int(gap.total_seconds()))
        connection.execute(
            sa.update(table).where(table.c.id == position_id).values(locator=locator)
        )

    def _into_chapter(
        self,
        connection: Connection,
        doc: _Doc,
        version: store.VersionRow,
        page: int,
        percentage: float,
        token: str,
    ) -> tuple[str, dict[str, Any], float] | None:
        """The page of the block as (chapter file, locator, share of the chapter)."""
        total = doc.version_pages
        if not within_guard(page, total, percentage):
            self._refused(token, "integrity")  # the open file is not the one whose digest was sent
            return None
        for member in self._members(connection, version.id):
            if member.first_page <= page < member.first_page + member.page_count:
                if member.item_id is None or member.fp is None:
                    self._refused(token, "chapter_gone")  # purged since the version: an orphan
                    return None
                inner = page - member.first_page + 1
                if not self._still_the_member(connection, member):
                    # Replaced since the version (I30): its page is a page of a file that is no
                    # longer there. The share of the chapter is kept, as a proportion (no page),
                    # and it is never handed to Kavita nor given back as a page of the block.
                    return member.item_id, {"transposed": True}, inner / member.page_count
                return member.item_id, {"page": inner}, inner / member.page_count
        self._refused(token, "no_chapter")
        return None

    # -- the read ---------------------------------------------------------------------------

    def pull(self, *, user_id: str, document: str, token: str = "") -> Pulled | None:
        """The furthest position, in the frame of the document asked; None when nothing is known
        or nothing can be told in this frame. The page of a block that is given is remembered for
        the token (`_returned`): the next PUT that sends it back is an echo, not a reading."""
        with self._engine.connect() as connection:
            try:
                doc = self._resolve(connection, user_id, document)
            except AmbiguousDocumentError:
                return None  # nothing is guessed: the answer is the one of nothing known
            if doc is None:
                return None
            return self._furthest(connection, user_id, doc, token)

    def _furthest(
        self, connection: Connection, user_id: str, doc: _Doc, token: str | None
    ) -> Pulled | None:
        """What `pull` gives for a document resolved; `token=None` reads it without remembering
        the page (the question "what would a GET give now?", asked by a PUT)."""
        order: list[tuple[str, _Member | None]]
        if doc.version is not None:
            members = self._members(connection, doc.version.id)
            order = [(m.unit_id, m) for m in reversed(members) if m.unit_id]
        elif doc.item_id is not None:
            units = connection.execute(
                sa.select(export.unit_coverage.c.unit_id)
                .where(
                    export.unit_coverage.c.carrier_type == "export_item",
                    export.unit_coverage.c.carrier_id == doc.item_id,
                )
                .order_by(export.unit_coverage.c.position.desc())
            ).scalars()
            order = [(str(u), None) for u in units]
        else:
            return None
        if not order:
            return None
        rows = {
            r.unit_id: r
            for r in progress_of(
                connection, viewer_id=user_id, subject_id=user_id,
                unit_ids=[str(u) for u, _m in order], proven=True,  # never a guess
            )
        }  # fmt: skip
        for unit_id, member in order:
            row = rows.get(unit_id)
            if row is None:
                continue
            position = connection.execute(
                sa.select(reading.reading_position).where(
                    reading.reading_position.c.id == row.last_position_id,
                    reading.reading_position.c.user_id == user_id,
                )
            ).first()
            if position is None or position.device_id == _DELIVERY:
                continue
            found = self._in_frame(connection, doc, member, position)  # the furthest decides
            if found is not None and doc.version is not None and token is not None:
                self._remember(user_id, token, doc.version.id, int(found.progress))
            return found
        return None

    def _remember(self, user_id: str, token: str, version_id: str, page: int) -> None:
        key = (user_id, token.lower())
        if key not in self._returned and len(self._returned) >= _RETURNED_BOUND:
            del self._returned[next(iter(self._returned))]
        self._returned[key] = (version_id, page, self._now())

    def _in_frame(
        self, connection: Connection, doc: _Doc, member: _Member | None, position: Any
    ) -> Pulled | None:
        if position.carrier_type != "export_item":
            return None
        locator = position.locator
        if doc.version is not None:
            if member is None:
                return None
            page = locator.get("page")
            if (
                position.carrier_id != member.item_id
                or not isinstance(page, int)
                or isinstance(page, bool)
                or not 1 <= page <= member.page_count
                or position.content_hash != self._anchor(connection, str(member.item_id))
                or not self._still_the_member(connection, member)
                or (position.source == "server" and member.state != "verified")
            ):
                return None  # a page Kavita counts is given only where it counts as we do (L50)
            of_block = member.first_page + page - 1
            progress, percentage = str(of_block), of_block / doc.version_pages
        else:
            if position.carrier_id != doc.item_id or position.content_hash != self._anchor(
                connection, str(doc.item_id)
            ):
                return None
            page = locator.get("page")
            if isinstance(page, int) and not isinstance(page, bool):
                progress = str(page)
            elif isinstance(locator.get("xpointer"), str):
                progress = locator["xpointer"]
            else:
                return None
            if position.percent is None:
                return None
            percentage = float(position.percent)
        when = position.server_ts
        when = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
        stamp = int(min(when, self._now()).timestamp())
        device = locator.get("device")
        if not isinstance(device, str) or not device:
            device = "Kavita" if position.source == "server" else "Shelfhold"
        return Pulled(doc.digest, progress, percentage, device, position.device_id, stamp)


def within_guard(page: int, total: int, percentage: float) -> bool:
    """Whether `page` of `total` and the share the device sent are of one file. KOReader sends
    `Math.roundPercent(page / pages)`, floored to the ten-thousandth, so the comparison is made in
    the space of the share: the floor, plus 2 pages or 0.5 % (whichever is wider) of slack."""
    return abs(page / total - percentage) <= GUARD_FLOOR + max(2.0 / total, 0.005)


def _page(text: str) -> int:
    """The page of a paged file: a whole number from 1, written in ASCII digits."""
    stripped = text.strip()
    if not _DIGITS.fullmatch(stripped):
        raise InvalidProgressError("page is a whole number")
    page = int(stripped)
    if not 1 <= page <= MAX_PAGE:
        raise InvalidProgressError("page is a whole number from 1")
    return page


def _xpointer(text: str) -> str:
    """The place in a reflowable document, kept as it is: text, bounded, with no control
    character (it is returned to the device and read back as JSON, never interpreted)."""
    return _clean(text, limit=MAX_XPOINTER)
