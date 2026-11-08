"""Passthrough storage: the received CBZ is filed in /library as it came (EF-BIB-006, EF-BIB-027).

In passthrough mode Shelfhold keeps `unit.json`, the hashes and the registry of copies, and no
third copy: nothing goes to /store. The received file is audited first, through `safe_open_archive`
and the CBZ allowlist (ENF-SEC-002, EF-BIB-051), after a size check that happens before the file
is even opened. Then `fsops` places it (atomic, lease checked, never overwriting), the database
records it in one transaction, and `unit.json` is projected after the commit.

The rendering (ComicInfo for a destination) is the export's job: here the file stays byte for byte
what the service sent, `original_sha256` says which bytes those are, and `content_hash` says what
payload they carry.
"""

import hashlib
import re
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any, Final, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.archives import DEFAULT_LIMITS, ArchiveLimits, safe_open_archive
from shelfhold.infrastructure.content_trust import check_cbz_entries
from shelfhold.infrastructure.fsops import CopyVerificationError, Fsops, FsopsError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, storage
from shelfhold.library.adapters.meta_projection import MetaProjection
from shelfhold.library.domain.content_hash import CONTENT_HASH_BY, FILE_HASH_BY, content_hash
from shelfhold.library.domain.copies import CopyFacts, is_independent
from shelfhold.library.domain.failures import (
    ReceivedTooLargeError,
    SafeguardFailedError,
    TombstonedContentError,
    VariantAlreadyStoredError,
)
from shelfhold.library.domain.store_budget import Candidate
from shelfhold.library.replica import (
    library_copies,
    record_library_rewrite,
    record_replica,
    record_store_observation,
    sibling_inodes,
    store_copies,
)
from shelfhold.library.tombstones.values import ContentHash

CBZ_MEDIA_TYPE: Final = "application/vnd.comicbook+zip"
DEFAULT_MAX_RECEIVED_BYTES: Final = 512 * 1024 * 1024


class ContentTombstones(Protocol):
    """The part of `TombstoneService` a store needs: is this file's content tombstoned?"""

    def check_content(
        self,
        hashes: Iterable[ContentHash] = (),
        *,
        hash_with: Callable[[str], str | None] | None = None,
    ) -> bool: ...


# local path -> (physical root id, path relative to that root); the composition root provides it
RootLocator = Callable[[str], tuple[str, str]]


@dataclass(frozen=True)
class Ingested:
    content_id: str
    replica_id: str
    original_sha256: str
    content_hash: str
    size: int
    page_count: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


class PassthroughStore:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        locate: RootLocator,
        data_dir: Path,
        *,
        max_received_bytes: int = DEFAULT_MAX_RECEIVED_BYTES,
        limits: ArchiveLimits = DEFAULT_LIMITS,
        tombstones: ContentTombstones,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._locate = locate
        self._projection = MetaProjection(engine, data_dir)
        self._max = max_received_bytes
        self._limits = limits
        self._tombstones = tombstones

    def ingest(
        self,
        *,
        unit_id: str,
        variant_id: str,
        staged: Path,
        dest: str,
        expected_sha256: str | None = None,
        upstream_ref: str | None = None,
    ) -> Ingested:
        """File `staged` at the local path `dest` in /library. Raises, having changed nothing,
        for a file too large, off the allowlist, not what was announced, or already there."""
        if staged.stat().st_size > self._max:
            raise ReceivedTooLargeError(f"{staged.stat().st_size} bytes, above {self._max}")
        with safe_open_archive(staged, name=PurePath(dest).name, limits=self._limits) as archive:
            listing = check_cbz_entries(archive)
            payload_hash = content_hash(archive)
        original = sha256_file(staged)
        if self._tombstones.check_content(
            [ContentHash(CONTENT_HASH_BY, payload_hash), ContentHash(FILE_HASH_BY, original)]
        ):
            raise TombstonedContentError("the content was purged: it is not filed again")
        if expected_sha256 is not None and expected_sha256 != original:
            raise ValueError("received_hash_mismatch: the file is not what the download counted")
        with self._engine.connect() as connection:
            taken = connection.execute(
                sa.select(storage.content.c.id).where(
                    storage.content.c.variant_id == variant_id,
                    storage.content.c.media_type == CBZ_MEDIA_TYPE,
                )
            ).first()
        if taken is not None:
            raise VariantAlreadyStoredError(f"the Variant {variant_id} already has its archive")

        root_id, rel = self._locate(dest)
        with staged.open("rb") as stream:
            identity = self._fsops.write_atomic(
                dest, stream, parents=True, replace=False,
                # read back before it is put in place: the copy is the file that was audited
                verify_digest=lambda: original.removeprefix("sha256:"),
            )  # fmt: skip
        device = self._fsops.device_of(dest)
        now = datetime.now(UTC)
        content_id = new_ulid()
        with self._engine.begin() as connection:
            connection.execute(
                sa.insert(storage.content).values(
                    id=content_id, unit_id=unit_id, variant_id=variant_id, kind="images",
                    media_type=CBZ_MEDIA_TYPE, format="cbz", retention="passthrough",
                    original_sha256=original, content_hash=payload_hash,
                    content_hash_by=CONTENT_HASH_BY, store_provenance="received",
                    size=identity.size, page_count=len(listing.images),
                )
            )  # fmt: skip
            replica_id = record_replica(
                connection, unit_id=unit_id, location="library", content_hash=payload_hash,
                file_hash=original, independent=True, mutable_by_third_party=True,
                physical_root_id=root_id, rel_path=rel, dev=device, ino=identity.ino,
                size=identity.size, mtime_ns=identity.mtime_ns, verified_at=now,
            )  # fmt: skip
            if upstream_ref is not None:  # the origin still has it: a copy, not ours to keep
                record_replica(
                    connection, unit_id=unit_id, location="upstream", content_hash=payload_hash,
                    file_hash=original, independent=True, mutable_by_third_party=True,
                    remote_ref=upstream_ref, size=identity.size, verified_at=now,
                )  # fmt: skip
        self._projection.project(unit_id)  # after the commit: the database is the authority
        return Ingested(
            content_id, replica_id, original, payload_hash, identity.size, len(listing.images)
        )


# -- keeping a copy (EF-BIB-027) ---------------------------------------------------------------


class IntentLog(Protocol):
    """The part of the intent journal a copy to /store needs (`export` owns the journal)."""

    def record(
        self,
        *,
        op: str,
        src: str | None = None,
        dst: str | None = None,
        expected_sha256: str | None = None,
        expected_file_id: Any = None,
    ) -> str: ...

    def mark_applying(self, intent_id: str) -> None: ...

    def mark_done(self, intent_id: str, *, within: Connection | None = None) -> None: ...

    def cancel(self, intent_id: str) -> None: ...


@dataclass(frozen=True)
class Derivative:
    original_sha256: str  # the bytes as received, kept in /store
    derived_sha256: str  # the canonical derivative that /library holds from now on
    derived_by: str  # `processor@version`


@dataclass(frozen=True)
class Kept:
    unit_id: str
    rel_path: str  # under /store
    method: Literal["reflink", "copy", "extracted"]
    file_hash: str


def keep_rel(unit_id: str, content_id: str) -> str:
    return f"units/{unit_id[:2]}/{unit_id}/{content_id}.cbz"


def keep_candidates(engine: Engine) -> list[Candidate]:
    """Units still in passthrough that a copy in /store is owed for, oldest content first:

    - those a shelf in `keep` mode asks to keep once they are exported (GAP-5-22); switching a shelf
      to `keep` proposes this catch-up and new Units of a keep shelf take the same road;
    - those of an Edition with **no shelf** (EF-EXP-046, "À ranger" and "À classer"): nothing is
      exported, so the archive received is the only copy and /store keeps it in the format received.
    """
    content, item, record = storage.content, export.export_item, export.export_record
    destination, shelf = export.shelf_destination, export.shelf
    unit, edition = catalog.unit, catalog.edition
    shelved = (
        sa.select(content.c.unit_id, sa.func.max(content.c.size).label("size"), content.c.id)
        .select_from(
            content.join(item, item.c.content_id == content.c.id)
            .join(record, record.c.export_item_id == item.c.id)
            .join(destination, destination.c.id == record.c.shelf_destination_id)
            .join(shelf, shelf.c.id == destination.c.shelf_id)
        )
        .where(
            content.c.retention == "passthrough",
            content.c.unit_id.is_not(None),
            shelf.c.retention_mode == "keep",
        )
        .group_by(content.c.id)
        .order_by(content.c.id)
    )
    unshelved = (
        sa.select(content.c.unit_id, content.c.size, content.c.id)
        .select_from(
            content.join(unit, unit.c.id == content.c.unit_id).join(
                edition, edition.c.id == unit.c.edition_id
            )
        )
        .where(
            content.c.retention == "passthrough",
            edition.c.shelf_id.is_(None),
            ~sa.exists().where(item.c.edition_id == edition.c.id),  # never exported: nothing else
        )
        .order_by(content.c.id)
    )
    found: dict[str, Candidate] = {}
    with engine.connect() as connection:
        for query in (shelved, unshelved):
            for r in connection.execute(query):
                found.setdefault(r.id, Candidate(r.unit_id, int(r.size)))
    return list(found.values())


class KeepCopier:
    """Keep a copy of a Unit in /store, as an independent copy: never a link (EF-BIB-027, D9).

    The file in /library, when it is still byte for byte what was received, is copied by
    `Fsops.copy_independent` (a reflink clone if the file system allows it, else a copy flushed
    to disk and read back), under a `copy_cross_root` intent: after a crash the target is absent
    (nothing happened) or complete, and `complete_interrupted_keep` finishes the bookkeeping at
    the next start. A library file that was rendered since (ComicInfo for a destination) is not a
    copy of the original: `extract` (the safeguard) strips the rendering and verifies the payload
    instead, and /store never holds a rendered file (I25)."""

    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        locate: RootLocator,
        store_dir: str,
        data_dir: Path,
        journal: IntentLog,
        *,
        extract: Callable[[str], Any] | None = None,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._locate = locate
        self._store = store_dir.rstrip("/")
        self._journal = journal
        self._extract = extract
        self._projection = MetaProjection(engine, data_dir)

    def keep(self, unit_id: str) -> Kept:
        with self._engine.connect() as connection:
            content = connection.execute(
                sa.select(storage.content)
                .where(
                    storage.content.c.unit_id == unit_id,
                    storage.content.c.retention == "passthrough",
                )
                .order_by(storage.content.c.id)
            ).first()
            if content is None:
                raise SafeguardFailedError("no_content", "nothing in passthrough to keep")
            copies = list(library_copies(connection, unit_id))
        for copy in copies:
            with suppress(FileNotFoundError, FsopsError):
                if self._fsops.sha256_of(copy.local) == content.original_sha256:
                    return self._copy(unit_id, content, copy.local)
        if self._extract is None:
            raise SafeguardFailedError("library_differs_from_original")
        result = self._extract(unit_id)
        return Kept(unit_id, result.rel_path, "extracted", result.file_hash)

    def declare_derivative(self, unit_id: str, derived: Path, derived_by: str) -> Derivative:
        """A source declared a descrambling (EF-BIB-006): `derived` is the canonical file, the
        bytes as received stay in /store, both traced by hash.

        The original is copied to /store first (an independent copy, under its intent); only then
        is the derivative written over the /library file, atomically and read back. A crash in
        between leaves the original kept and /library unchanged: the same call, repeated, goes on
        from there. The derivative is audited as any archive is before it replaces anything."""
        with self._engine.connect() as connection:
            content = connection.execute(
                sa.select(storage.content)
                .where(
                    storage.content.c.unit_id == unit_id,
                    storage.content.c.retention.in_(("passthrough", "keep")),
                    storage.content.c.derived_hash.is_(None),
                )
                .order_by(storage.content.c.id)
            ).first()
            if content is None:
                raise SafeguardFailedError("no_content", "nothing to derive")
            copies = list(library_copies(connection, unit_id))
        with safe_open_archive(derived, name="derived.cbz") as archive:
            check_cbz_entries(archive)
        derived_hash = sha256_file(derived)
        if content.retention == "passthrough":
            self.keep(unit_id)  # the original first, byte for byte
        target = next(
            (c for c in copies if self._fsops.sha256_of(c.local) == content.original_sha256), None
        )
        if target is None:
            raise SafeguardFailedError("library_differs_from_original")
        with derived.open("rb") as stream:
            written = self._fsops.write_atomic(
                target.local, stream, replace=True,
                verify_digest=lambda: derived_hash.removeprefix("sha256:"),
            )  # fmt: skip
        with self._engine.begin() as connection:
            record_library_rewrite(
                connection, target.replica_id, file_hash=derived_hash,
                dev=self._fsops.device_of(target.local), ino=written.ino, size=written.size,
                mtime_ns=written.mtime_ns, verified_at=datetime.now(UTC),
            )  # fmt: skip
            connection.execute(
                sa.update(storage.content)
                .where(storage.content.c.id == content.id)
                .values(derived_hash=derived_hash, derived_by=derived_by)
            )
        self._projection.project(unit_id)
        return Derivative(content.original_sha256, derived_hash, derived_by)

    def _copy(self, unit_id: str, content: Any, source: str) -> Kept:
        rel = keep_rel(unit_id, content.id)
        target = f"{self._store}/{rel}"
        seen = self._fsops.observe(source)
        intent_id = self._journal.record(
            op="copy_cross_root", src=source, dst=target,
            expected_sha256=content.original_sha256,
            expected_file_id=seen[1] if seen else None,
        )  # fmt: skip
        self._journal.mark_applying(intent_id)
        try:
            done = self._fsops.copy_independent(
                source, target, parents=True, replace=True,
                expected_sha256=content.original_sha256.removeprefix("sha256:"),
            )  # fmt: skip
        except CopyVerificationError as error:  # the library file changed since it was looked at
            self._journal.cancel(intent_id)
            raise SafeguardFailedError("library_differs_from_original", str(error)) from error
        except BaseException:
            self._journal.cancel(intent_id)  # staged and atomic: nothing landed
            raise
        with self._engine.begin() as connection:
            independent = complete_keep(
                connection, self._fsops, self._locate, content.id, target, source,
                datetime.now(UTC),
            )  # fmt: skip
            self._journal.mark_done(intent_id, within=connection)
        if not independent:  # impossible by construction; said, not hidden
            raise SafeguardFailedError("not_independent", "the copy shares an inode")
        self._projection.project(unit_id)
        return Kept(unit_id, rel, "reflink" if done.reflinked else "copy", "sha256:" + done.sha256)


def complete_keep(
    connection: Connection,
    fsops: Fsops,
    locate: RootLocator,
    content_id: str,
    target: str,
    source: str | None,
    now: datetime,
) -> bool:
    """The bookkeeping of a copy that is on disk: the content is kept, the registry knows the
    copy. Whether it counts is decided here, from the disk (distinct inode, hash read now), never
    from the way it was made. Returns that verdict."""
    row = connection.execute(
        sa.select(storage.content).where(
            storage.content.c.id == content_id, storage.content.c.retention == "passthrough"
        )
    ).first()
    if row is None:  # already completed: a replayed intent changes nothing
        return True
    found = fsops.observe(target)
    if found is None:
        raise FileNotFoundError(target)
    identity = found[1]
    device = fsops.device_of(target)
    seen_hash = fsops.sha256_of(target)
    siblings = sibling_inodes(connection, row.unit_id)
    if source is not None and (origin := fsops.observe(source)) is not None:
        siblings.add((fsops.device_of(source), origin[1].ino))  # the file it was copied from
    independent = is_independent(
        CopyFacts(device, identity.ino, seen_hash),
        stored_sha256=row.original_sha256, siblings=siblings,
    )  # fmt: skip
    rel = keep_rel(row.unit_id, content_id)
    connection.execute(
        sa.update(storage.content)
        .where(storage.content.c.id == content_id)
        .values(retention="keep", path=rel, store_provenance="received")
    )
    root_id, root_rel = locate(target)
    record_replica(
        connection, unit_id=row.unit_id, location="store", content_hash=row.content_hash,
        file_hash=seen_hash, independent=independent, physical_root_id=root_id,
        rel_path=root_rel, dev=device, ino=identity.ino, size=identity.size,
        mtime_ns=identity.mtime_ns, verified_at=now,
    )  # fmt: skip
    return independent


def complete_interrupted_keep(
    connection: Connection,
    fsops: Fsops,
    locate: RootLocator,
    *,
    src: str | None,
    dst: str,
    now: datetime,
) -> None:
    """For the intent journal's replay at start-up: a `copy_cross_root` whose target is a kept
    copy (`.../units/<xx>/<unit>/<content>.cbz` in /store) gets its bookkeeping completed. Any
    other copy (a repatriation) is not ours: nothing happens."""
    match = _KEEP_NAME.search(dst)
    if match is None:
        return
    complete_keep(connection, fsops, locate, match["content"], dst, src, now)


_KEEP_NAME: Final = re.compile(r"/units/[0-9A-Z]{2}/[0-9A-Z]{26}/(?P<content>[0-9A-Z]{26})\.cbz\Z")


def verify_kept(engine: Engine, fsops: Fsops, unit_id: str) -> int:
    """Look at the Unit's /store copies again: each counts only with a distinct inode and the
    hash it was stored at. Returns how many count."""
    counted = 0
    now = datetime.now(UTC)
    with engine.begin() as connection:
        siblings = sibling_inodes(connection, unit_id)
        for copy in store_copies(connection, unit_id):
            try:
                found = fsops.observe(copy.local)
                seen = fsops.sha256_of(copy.local)
            except (FileNotFoundError, FsopsError):
                found, seen = None, None
            if found is None:
                record_store_observation(
                    connection, copy.replica_id, independent=False, dev=None, ino=None,
                    size=None, mtime_ns=None, verified_at=now,
                )  # fmt: skip
                continue
            identity, device = found[1], fsops.device_of(copy.local)
            good = is_independent(
                CopyFacts(device, identity.ino, seen or ""), stored_sha256=copy.file_hash,
                siblings=siblings,
            )  # fmt: skip
            counted += good
            record_store_observation(
                connection, copy.replica_id, independent=good, dev=device, ino=identity.ino,
                size=identity.size, mtime_ns=identity.mtime_ns, verified_at=now,
            )  # fmt: skip
    return counted
