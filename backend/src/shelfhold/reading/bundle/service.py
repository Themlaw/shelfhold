"""The block of a series, served (ADR-0053 §1 to §3, §8): the provider the OPDS route plugs in.

What the route has already decided is decided again here, for each request: the Edition is one the
reader's scope sees *now*, and a version is the profile's own (a version number is no capability).
Then, before the first byte, every chapter of the part is opened and checked against the manifest
(size and modification time of the file, then the central directory of its archive against what
the version recorded); a disagreement is a 410 and nothing is sent. A chapter whose file is not
there, is a link, climbs out of the root, or is not a CBZ of readable pages is left out, with a
reason, and the block is made of the rest.

A version is made at the first download of a content and reused after (same fingerprint); the feed
only reads the database. The KOReader digest of the version is computed from the layout, reading
twelve samples of one KiB, not the file (`LayoutStream`).
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.catalog.public import visible_edition_ids
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import catalog, identity
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.reading.bundle import plan as planning
from shelfhold.reading.bundle import store
from shelfhold.reading.bundle.errors import (
    BundleGoneError,
    BundleNotFoundError,
)
from shelfhold.reading.bundle.gate import Gate, Lease
from shelfhold.reading.bundle.limits import BundleLimits
from shelfhold.reading.bundle.model import BundleDownload, BundleOffer
from shelfhold.reading.bundle.plan import Candidate, Part
from shelfhold.reading.bundle.sources import (
    ChapterLeftOutError,
    ManifestMismatchError,
    OpenSource,
    Page,
    Scan,
    open_source,
)
from shelfhold.reading.bundle.stream import BundleStream
from shelfhold.reading.domain.bundle_zip import (
    BundleZip,
    LayoutStream,
    PartTooLargeError,
    ZipEntry,
    chapter_folder,
    page_name,
)
from shelfhold.reading.domain.document_hash import koreader_partial_md5

log = structlog.get_logger(__name__)

TITLE_SUFFIX = "Série complète"


@dataclass
class _Built:
    """A part with its chapters open and read: ready to be laid out."""

    members: list[Candidate]
    sources: list[OpenSource]
    scans: list[Scan]
    excluded: dict[str, int]

    def close(self) -> None:
        for source in self.sources:
            source.close()


@dataclass
class _Laid:
    """The entries of a part laid out: what the zip is made of, and its fingerprint."""

    entries: list[ZipEntry]
    refs: list[tuple[int, Page]]
    starts: set[int]
    rows: list[store.MemberRow]
    fp: str


class SeriesBundles:
    """Implements `interfaces.http_api.opds.bundle.SeriesBundleProvider`."""

    def __init__(
        self,
        engine: Engine,
        keyring: Callable[[], Keyring],
        *,
        now: Callable[[], datetime],
        limits: BundleLimits | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine, self._keyring, self._now = engine, keyring, now
        self.limits = limits or BundleLimits()
        self._monotonic = monotonic
        self._gate = Gate(
            per_token=self.limits.per_token,
            per_instance=self.limits.per_instance,
            send_timeout_s=self.limits.send_timeout_s,
            max_duration_s=self.limits.max_duration_s,
            monotonic=monotonic,
        )

    # -- the feed: the database only ------------------------------------------------------------
    def offers(self, *, user_id: str, scope: Any, edition_id: str) -> list[BundleOffer]:
        with self._engine.connect() as connection:
            if edition_id not in visible_edition_ids(connection, scope, [edition_id]):
                return []
            candidates = planning.load_candidates(connection, edition_id)
            eligible = [c for c in candidates if c.left_out is None]
            parts = planning.plan_parts(
                planning.stored_boundaries(connection, edition_id), eligible, self.limits
            )
            left_out = len(candidates) - len(eligible)
            title = self._title(connection, edition_id)
            found = []
            for part in parts:
                held = store.latest(connection, edition_id, part.index, user_id)
                current = held if held is not None and held.state_key == part.state_key else None
                number = current.number if current else (held.number if held else 0) + 1
                excluded = (
                    sum(current.excluded.values())
                    if current
                    else (left_out if part.index == 1 else 0)
                )
                found.append(
                    BundleOffer(
                        filename=_filename(title, part.index, len(parts), number),
                        size=current.size if current else None,
                        part=part.index,
                        excluded=excluded,
                    )
                )
            return found

    def _title(self, connection: sa.Connection, edition_id: str) -> str:
        e, w = catalog.edition, catalog.work
        row = connection.execute(
            sa.select(w.c.title, e.c.lang, e.c.work_id)
            .select_from(e.join(w, w.c.id == e.c.work_id))
            .where(e.c.id == edition_id)
        ).one()
        siblings = connection.execute(
            sa.select(sa.func.count()).select_from(e).where(e.c.work_id == row.work_id)
        ).scalar_one()
        return f"{row.title} ({row.lang})" if siblings > 1 else str(row.title)

    # -- a block by its edition and part ---------------------------------------------------------
    def open(
        self,
        *,
        user_id: str,
        scope: Any,
        edition_id: str,
        part: int = 1,
        token_id: str | None = None,
    ) -> BundleDownload:
        with self._engine.connect() as connection:
            if edition_id not in visible_edition_ids(connection, scope, [edition_id]):
                raise BundleNotFoundError("edition")
            candidates = planning.load_candidates(connection, edition_id)
            title = self._title(connection, edition_id)
        eligible = [c for c in candidates if c.left_out is None]
        with write_transaction(self._engine) as connection:
            parts = planning.plan_parts(
                planning.stored_boundaries(connection, edition_id), eligible, self.limits
            )
            alias_items = planning.ensure_partitions(connection, edition_id, parts, self._now())
        if not 1 <= part <= len(parts):
            raise BundleNotFoundError("part")
        chosen = parts[part - 1]
        left_out: dict[str, int] = {}
        for c in candidates:
            if c.left_out is not None and part == 1:
                left_out[c.left_out] = left_out.get(c.left_out, 0) + 1
        lease = self._gate.acquire(token_id)
        try:
            built = self._open_members(chosen.members, left_out)
            return self._serve(
                built,
                user_id=user_id,
                edition_id=edition_id,
                title=title,
                part=chosen,
                total=len(parts),
                alias_item_id=alias_items[chosen.index],
                token_id=token_id,
                lease=lease,
            )
        except BaseException:
            lease.release()
            raise

    # -- a block by its version -----------------------------------------------------------------
    def open_version(
        self, *, user_id: str, scope: Any, version_id: str, token_id: str | None = None
    ) -> BundleDownload:
        with self._engine.connect() as connection:
            version = store.by_id(connection, version_id)
            if version is None or version.profile_user_id != user_id:
                raise BundleNotFoundError("version")  # someone else's is an absent one
            if version.edition_id not in visible_edition_ids(
                connection, scope, [version.edition_id]
            ):
                raise BundleNotFoundError("version")
            if version.purged:
                raise BundleGoneError("purged")
            rows = store.members_of(connection, version_id)
            records = {
                r.item_id: r
                for r in planning.load_candidates(connection, version.edition_id)
                if any(r.item_id == m.item_id for m in rows)
            }
            title = self._title(connection, version.edition_id)
            total = len(
                planning.plan_parts(
                    planning.stored_boundaries(connection, version.edition_id),
                    [
                        c
                        for c in planning.load_candidates(connection, version.edition_id)
                        if c.left_out is None
                    ],
                    self.limits,
                )
            )
        key = store.fingerprint_key(self._keyring())
        members: list[Candidate] = []
        for row in rows:
            held = records.get(row.item_id) if row.item_id else None
            if held is None or row.fp is None or row.fp != store.member_fp(key, held.file_hash):
                raise BundleGoneError("a chapter is gone or was replaced")
            members.append(held)
        lease = self._gate.acquire(token_id)
        try:
            built = self._open_members(members, {}, strict=True)
            for scan, row in zip(built.scans, rows, strict=True):
                if scan.digest != row.pages_digest:
                    raise BundleGoneError("a chapter no longer holds what the version recorded")
            part = Part(version.part_index, tuple(members), None)
            return self._serve(
                built,
                user_id=user_id,
                edition_id=version.edition_id,
                title=title,
                part=part,
                total=max(total, version.part_index),
                alias_item_id=None,
                token_id=token_id,
                lease=lease,
                known=version,
            )
        except BaseException:
            lease.release()
            raise

    # -- retention ------------------------------------------------------------------------------
    def prune(self, now: datetime | None = None) -> int:
        return prune_versions(self._engine, now or self._now(), self.limits)

    # -- internals ------------------------------------------------------------------------------
    def _open_members(
        self, members: Sequence[Candidate], left_out: dict[str, int], *, strict: bool = False
    ) -> _Built:
        built = _Built([], [], [], dict(left_out))
        try:
            for candidate in members:
                try:
                    source = self._open_one(candidate)
                except ChapterLeftOutError as why:
                    if strict:
                        raise BundleGoneError(f"a chapter is {why.reason}") from None
                    built.excluded[why.reason] = built.excluded.get(why.reason, 0) + 1
                    continue
                try:
                    scan = source.scan(self.limits)
                except ChapterLeftOutError as why:
                    source.close()
                    if strict:
                        raise BundleGoneError(f"a chapter is {why.reason}") from None
                    built.excluded[why.reason] = built.excluded.get(why.reason, 0) + 1
                    continue
                built.members.append(candidate)
                built.sources.append(source)
                built.scans.append(scan)
        except BaseException:
            built.close()
            raise
        if not built.members:
            raise BundleGoneError("nothing to serve")
        return built

    def _open_one(self, candidate: Candidate) -> OpenSource:
        assert candidate.root is not None  # noqa: S101  (eligible candidates have a root)
        source = open_source(candidate.root, candidate.rel_path)
        try:
            if (candidate.size is not None and source.size != candidate.size) or (
                candidate.mtime_ns is not None and source.mtime_ns != candidate.mtime_ns
            ):
                raise ManifestMismatchError(candidate.item_id)
        except ManifestMismatchError:
            source.close()
            raise BundleGoneError("a file does not match the manifest") from None
        return source

    def _assemble(self, built: _Built, key: bytes) -> "_Laid":
        entries: list[ZipEntry] = []
        refs: list[tuple[int, Page]] = []
        starts: set[int] = set()
        rows: list[store.MemberRow] = []
        first_page = 1
        for rank, (candidate, scan) in enumerate(zip(built.members, built.scans, strict=True), 1):
            folder = chapter_folder(rank, candidate.number)
            starts.add(len(entries))
            for number, page in enumerate(scan.pages, start=1):
                entries.append(
                    ZipEntry(f"{folder}/{page_name(number, page.ext)}", page.size, page.crc)
                )
                refs.append((rank - 1, page))
            rows.append(
                store.MemberRow(
                    rank,
                    candidate.item_id,
                    candidate.unit_id,
                    candidate.number,
                    store.member_fp(key, candidate.file_hash),
                    scan.digest,
                    first_page,
                    len(scan.pages),
                    scan.canonical,
                )
            )
            first_page += len(scan.pages)
        return _Laid(entries, refs, starts, rows, store.content_fp(key, entries))

    def _checked(
        self, built: _Built, key: bytes, laid: "_Laid", edition_id: str, part: Part, user_id: str
    ) -> "_Laid":
        """A version about to be made reads every page once, and a chapter whose archive lies
        (a CRC the data does not have) is left out with the others served, instead of cutting
        every download at the same page. A content already made was checked then."""
        with self._engine.connect() as connection:
            if store.reusable(connection, edition_id, part.index, user_id, laid.fp) is not None:
                return laid
        bad = [i for i, (src, scan) in enumerate(zip(built.sources, built.scans, strict=True))
               if not src.pages_match(scan, self.limits.chunk_bytes)]  # fmt: skip
        if not bad:
            return laid
        for i in reversed(bad):
            built.sources.pop(i).close()
            built.members.pop(i)
            built.scans.pop(i)
        built.excluded["unreadable"] = built.excluded.get("unreadable", 0) + len(bad)
        log.warning("opds.bundle.chapters_left_out", reason="crc", count=len(bad))
        if not built.members:
            raise BundleGoneError("nothing to serve")
        return self._assemble(built, key)

    def _serve(
        self,
        built: _Built,
        *,
        user_id: str,
        edition_id: str,
        title: str,
        part: Part,
        total: int,
        alias_item_id: str | None,
        token_id: str | None,
        lease: Lease,
        known: store.VersionRow | None = None,
    ) -> BundleDownload:
        key = store.fingerprint_key(self._keyring())
        laid = self._assemble(built, key)
        if known is None:
            laid = self._checked(built, key, laid, edition_id, part, user_id)
        entries, refs, starts, rows, fp = laid.entries, laid.refs, laid.starts, laid.rows, laid.fp
        try:
            layout = BundleZip(entries, bytes.fromhex(fp))
        except PartTooLargeError:
            raise BundleGoneError("the part is over the size a reader can take") from None
        if known is not None and (known.content_fp != fp or known.size != layout.size):
            raise BundleGoneError("the block no longer is what the version was")
        version = known or self._record(
            built, layout, refs, rows, fp, edition_id, part, alias_item_id, user_id
        )
        if known is not None:
            with write_transaction(self._engine) as connection:
                store.mark_served(connection, known.id, self._now())
        stream = BundleStream(
            layout,
            built.sources,
            refs,
            starts,
            limits=self.limits,
            token_alive=self._token_alive(token_id),
            still_visible=self._visible(user_id, edition_id),
            lease=lease,
            monotonic=self._monotonic,
        )
        return BundleDownload(
            filename=_filename(title, part.index, total, version.number),
            size=layout.size,
            chunks=stream,
            version_id=version.id,
            content_fp=fp,
        )

    def _record(
        self,
        built: _Built,
        layout: BundleZip,
        refs: list[tuple[int, Page]],
        rows: list[store.MemberRow],
        fp: str,
        edition_id: str,
        part: Part,
        alias_item_id: str | None,
        user_id: str,
    ) -> store.VersionRow:
        assert alias_item_id is not None  # noqa: S101
        with self._engine.connect() as connection:
            same = store.reusable(connection, edition_id, part.index, user_id, fp)
        if same is not None:
            with write_transaction(self._engine) as connection:
                store.mark_served(connection, same.id, self._now())
                store.heal(connection, same, rows, alias_item_id)
            return same

        def read_entry(index: int, start: int, length: int) -> bytes:
            source_index, page = refs[index]
            return built.sources[source_index].read_range(page, start, length)

        md5 = "md5:" + koreader_partial_md5(LayoutStream(layout, read_entry))  # type: ignore[arg-type]
        now = self._now()
        with write_transaction(self._engine) as connection:
            same = store.reusable(connection, edition_id, part.index, user_id, fp)
            if same is not None:
                store.mark_served(connection, same.id, now)
                store.heal(connection, same, rows, alias_item_id)
                return same
            made = store.create(
                connection,
                edition_id=edition_id,
                part_index=part.index,
                user_id=user_id,
                alias_item_id=alias_item_id,
                fp=fp,
                koreader_md5=md5,
                state_key=part.state_key,
                size=layout.size,
                entry_count=len(layout.entries),
                excluded=dict(built.excluded),
                members=rows,
                now=now,
            )
            store.prune_one_profile(connection, edition_id, part.index, user_id, now, self.limits)
            return made

    def _token_alive(self, token_id: str | None) -> Callable[[], bool]:
        if token_id is None:
            return lambda: True
        table = identity.device_token

        def alive() -> bool:
            with self._engine.connect() as connection:
                row = connection.execute(
                    sa.select(table.c.revoked_at, table.c.expires_at).where(table.c.id == token_id)
                ).first()
            if row is None or row.revoked_at is not None:
                return False
            return row.expires_at is None or self._now() < row.expires_at

        return alive

    def _visible(self, user_id: str, edition_id: str) -> Callable[[], bool]:
        def visible() -> bool:
            with self._engine.connect() as connection:
                scope = reader_scope(connection, user_id)
                return edition_id in visible_edition_ids(connection, scope, [edition_id])

        return visible


def _filename(title: str, part: int, parts: int, number: int) -> str:
    where = f" ({part} sur {parts})" if parts > 1 else ""
    return f"{title} — {TITLE_SUFFIX}{where} v{number}.cbz"


def prune_versions(engine: Engine, now: datetime, limits: BundleLimits | None = None) -> int:
    """Purge what the retention no longer keeps. Returns the number of versions purged."""
    with write_transaction(engine) as connection:
        return store.prune_all(connection, now, limits or BundleLimits())
