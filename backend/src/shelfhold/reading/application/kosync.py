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
    UnknownCarrierError,
    _current_hash,
    progress_of,
    record_position,
)
from shelfhold.reading.bundle import store
from shelfhold.reading.bundle.plan import LIVE

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
    """How many unknown digests each token sent (ADR-0053 §5): in memory, bounded, and it keeps
    the token's public identifier only, never what was asked."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def hit(self, token: str) -> None:
        token = token.lower()
        if token not in self._counts and len(self._counts) >= _UNKNOWN_BOUND:
            del self._counts[next(iter(self._counts))]
        self._counts[token] = self._counts.get(token, 0) + 1

    def of(self, token: str) -> int:
        return self._counts.get(token.lower(), 0)

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)


@dataclass(frozen=True)
class _Member:
    rank: int
    item_id: str | None
    unit_id: str | None
    fp: str | None
    first_page: int
    page_count: int


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
            _Member(m.rank, m.item_id, m.unit_id, m.fp, m.first_page, m.page_count)
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

    def _reflowable(self, connection: Connection, item_id: str) -> bool:
        found = connection.execute(
            sa.select(export.export_record.c.format)
            .where(export.export_record.c.export_item_id == item_id)
            .limit(1)
        ).scalar()
        return str(found or "").lower() in _REFLOWABLE

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
        """Records where the device is. True: stored. False: accepted without effect (unknown
        digest, a chapter that is gone, a percentage that is not of this file)."""
        device = _clean(device, limit=MAX_TEXT)
        device_id = None if device_id is None else _clean(device_id, limit=MAX_TEXT)
        if not isinstance(percentage, int | float) or not math.isfinite(percentage):
            raise InvalidProgressError("percentage")
        if not 0.0 <= percentage <= 1.0:
            raise InvalidProgressError("percentage")
        with write_transaction(self._engine) as connection:
            doc = self._resolve(connection, user_id, document)
            if doc is None:
                self.unknown.hit(token)
                log.info("kosync.unknown_document", token=token.lower())
                return False
            now = self._now()
            if doc.version is not None:
                target = self._into_chapter(
                    connection, doc, doc.version, progress, percentage, token
                )
                if target is None:
                    return False
                item_id, locator, percent = target
                traverse = True
            elif doc.item_id is not None:
                item_id = doc.item_id
                if self._reflowable(connection, item_id):
                    locator = {"xpointer": _xpointer(progress)}
                    traverse = False
                else:
                    locator = {"page": _page(progress)}
                    traverse = True
                percent = percentage
            else:
                return False
            anchor = self._anchor(connection, item_id)
            if anchor is None:
                return False
            locator["device"] = device
            try:
                record_position(
                    connection, reader_id=user_id, subject_id=user_id, carrier_type="export_item",
                    carrier_id=item_id, content_hash=anchor, locator=locator, percent=percent,
                    client_ts=now, device_id=device_id, now=now, traverse=traverse,
                )  # fmt: skip
            except UnknownCarrierError:
                return False
            return True

    def _into_chapter(
        self,
        connection: Connection,
        doc: _Doc,
        version: store.VersionRow,
        progress: str,
        percentage: float,
        token: str,
    ) -> tuple[str, dict[str, Any], float] | None:
        """The page of the block as (chapter file, locator, share of the chapter)."""
        page = _page(progress)
        total = doc.version_pages
        if page > total:
            raise InvalidProgressError("page beyond the block")
        n = page / percentage if percentage > 0 else math.inf
        if abs(n - total) > max(2.0, 0.005 * total):
            log.info("kosync.integrity_refused", token=token.lower(), version=version.id)
            return None  # the file that is open is not the one whose digest was sent
        for member in self._members(connection, version.id):
            if member.first_page <= page < member.first_page + member.page_count:
                if member.item_id is None or not self._still_the_member(connection, member):
                    log.info("kosync.orphan_position", token=token.lower(), rank=member.rank)
                    return None  # purged or replaced: a place with no chapter to be on
                inner = page - member.first_page + 1
                return member.item_id, {"page": inner}, inner / member.page_count
        return None

    # -- the read ---------------------------------------------------------------------------

    def pull(self, *, user_id: str, document: str) -> Pulled | None:
        """The furthest position, in the frame of the document asked; None when nothing is known
        or nothing can be told in this frame."""
        with self._engine.connect() as connection:
            doc = self._resolve(connection, user_id, document)
            if doc is None:
                return None
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
                    unit_ids=[str(u) for u, _m in order],
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
                return self._in_frame(connection, doc, member, position)  # the furthest decides
        return None

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
            ):
                return None
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
