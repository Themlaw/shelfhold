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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Final, Protocol

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.archives import DEFAULT_LIMITS, ArchiveLimits, safe_open_archive
from shelfhold.infrastructure.content_trust import check_cbz_entries
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import storage
from shelfhold.library.adapters.meta_projection import MetaProjection
from shelfhold.library.domain.content_hash import CONTENT_HASH_BY, content_hash
from shelfhold.library.domain.failures import (
    ReceivedTooLargeError,
    TombstonedContentError,
    VariantAlreadyStoredError,
)
from shelfhold.library.replica import record_replica
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
        tombstones: ContentTombstones | None = None,
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
        if self._tombstones is not None and self._tombstones.check_content(
            [ContentHash(CONTENT_HASH_BY, payload_hash)]
        ):
            raise TombstonedContentError("the content was purged: it is not filed again")
        original = sha256_file(staged)
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
