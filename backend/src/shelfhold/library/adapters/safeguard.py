"""Safeguard: put the original in /store (EF-BIB-038 mechanism, EF-BIB-045, EF-BIB-048, G1).

Where the bytes come from, in this order (`domain.source_choice`):

1. the origin, when it is still there: the file as received, `store_provenance = received`;
2. else the file in /library, if it is still the one the manifest knows: Shelfhold's rendering is
   stripped from it and the result is verified by `content_hash` before it enters /store,
   `store_provenance = extracted`.

A library file that no longer matches its manifest (a third party changed it) is never a source;
the registry is told what is really there, so `independent_copies` stays truthful. When the
`content_hash` does not verify, or the bytes are a rendering, nothing is written and the Unit stays
"irreplaceable without a kept copy" (`unit.safeguard_failed`). /store never holds a rendered file
(I25), and the copy is a distinct inode: never a hard link (EF-BIB-027).
"""

import hashlib
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final, Literal

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.archives import (
    ArchiveWriter,
    SafeArchive,
    UnsafeArchiveError,
    safe_open_archive,
)
from shelfhold.infrastructure.content_trust import UntrustedEntryError, check_cbz_entries
from shelfhold.infrastructure.fsops import Fsops, FsopsError
from shelfhold.infrastructure.schema import storage
from shelfhold.library.adapters.meta_projection import MetaProjection
from shelfhold.library.adapters.receive import Received
from shelfhold.library.adapters.storage import RootLocator, sha256_file
from shelfhold.library.domain.content_hash import content_hash
from shelfhold.library.domain.failures import SafeguardFailedError
from shelfhold.library.domain.source_choice import choose_source
from shelfhold.library.replica import (
    LibraryCopy,
    library_copies,
    record_replica,
    rendered_file_hashes,
    update_observation,
)

UpstreamFetch = Callable[[], AbstractContextManager[Received]]
_CHUNK: Final = 1 << 20


@dataclass(frozen=True)
class SafeguardResult:
    source: Literal["upstream", "library"]
    provenance: Literal["received", "extracted"]
    rel_path: str
    content_hash: str
    file_hash: str


def strip_rendering(archive: SafeArchive, out: BinaryIO) -> None:
    """The pages of an audited archive, in order, without ComicInfo or any other sidecar."""
    listing = check_cbz_entries(archive)
    with ArchiveWriter(out) as writer:
        for name in listing.images:
            with archive.open(name) as page:
                writer.add(name, page)


def _payload_hash(path_or_stream: Path | BinaryIO, name: str) -> str:
    with safe_open_archive(path_or_stream, name=name) as archive:
        check_cbz_entries(archive)
        return content_hash(archive)


class Safeguard:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        locate: RootLocator,
        store_dir: str,
        data_dir: Path,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._locate = locate
        self._store = store_dir.rstrip("/")
        self._data_dir = data_dir
        self._projection = MetaProjection(engine, data_dir)

    # -- the registry, brought up to what is on disk ------------------------------------------

    def _refresh_library(
        self, connection: Connection, unit_id: str, reference: str
    ) -> tuple[bool | None, list[LibraryCopy]]:
        """Look at each library copy. Returns (does a copy still match its manifest?, the copies).

        The manifest (`file_hash`, what the exporter wrote) is never rewritten: a file that is
        not that file stays "not the manifest's" on every later pass, so a third party's file can
        never become a source by being looked at twice. What is observed goes to `content_hash`
        (the payload it carries now, NULL if it is not a valid archive) and `independent` is
        recomputed from it."""
        copies = list(library_copies(connection, unit_id))
        matching: bool | None = None if not copies else False
        now = datetime.now(UTC)
        for copy in copies:
            try:
                seen = self._fsops.sha256_of(copy.local)
                identity = self._fsops.observe(copy.local)
            except (FileNotFoundError, FsopsError):
                update_observation(
                    connection, copy.replica_id, content_hash=copy.content_hash,
                    independent=False, verified_at=now,
                )  # fmt: skip
                continue
            size = identity[1].size if identity else None
            mtime_ns = identity[1].mtime_ns if identity else None
            if seen == copy.file_hash:
                matching = True
                update_observation(
                    connection, copy.replica_id, content_hash=reference, independent=True,
                    verified_at=now, size=size, mtime_ns=mtime_ns,
                )  # fmt: skip
                continue
            try:  # altered by a third party: what does it carry now?
                with self._fsops.open_read(copy.local) as stream:
                    payload = _payload_hash(stream, "x.cbz")
            except (UnsafeArchiveError, UntrustedEntryError, FsopsError):
                payload = None
            update_observation(
                connection, copy.replica_id, content_hash=payload,
                independent=payload is not None, verified_at=now, size=size, mtime_ns=mtime_ns,
            )  # fmt: skip
        return matching, copies

    # -- the safeguard ------------------------------------------------------------------------

    def run(self, unit_id: str, *, upstream: UpstreamFetch | None = None) -> SafeguardResult:
        with self._engine.begin() as connection:
            content = connection.execute(
                sa.select(storage.content)
                .where(
                    storage.content.c.unit_id == unit_id,
                    storage.content.c.retention == "passthrough",
                )
                .order_by(storage.content.c.id)
            ).first()
            if content is None or content.content_hash is None:
                raise SafeguardFailedError("no_content", "nothing in passthrough to keep")
            matching, copies = self._refresh_library(connection, unit_id, content.content_hash)
            rendered = rendered_file_hashes(connection, unit_id, original=content.original_sha256)
        choice = choose_source(
            upstream_available=upstream is not None, library_matches_manifest=matching
        )
        rel = f"units/{unit_id[:2]}/{unit_id}/{content.id}.cbz"
        local = f"{self._store}/{rel}"
        if choice.source == "library":
            matching_copy = next((c for c in copies if self._still_matches(c)), None)
            if matching_copy is None:
                raise SafeguardFailedError("library_differs_from_manifest")
            provenance = "extracted"
            with tempfile.TemporaryFile(dir=self._staging(), suffix=".cbz") as scratch:
                with (
                    self._fsops.open_read(matching_copy.local) as stream,
                    safe_open_archive(stream, name="x.cbz") as archive,
                ):
                    strip_rendering(archive, scratch)
                scratch.flush()
                scratch.seek(0)
                stored = self._place(local, scratch, content.content_hash)
        elif choice.source == "upstream" and upstream is not None:
            provenance = "received"
            with upstream() as received:
                self._check_received(received, content.content_hash, rendered)
                stored = self._place(local, received.path, content.content_hash)
        else:
            raise SafeguardFailedError(choice.reason)
        self._record(unit_id, content.id, rel, local, provenance, stored, content.content_hash)
        self._projection.project(unit_id)
        return SafeguardResult(choice.source, provenance, rel, content.content_hash, stored)  # type: ignore[arg-type]

    # -- internals ----------------------------------------------------------------------------

    def _staging(self) -> Path:
        staging = self._data_dir / "tmp"
        return staging if staging.is_dir() else self._data_dir

    def _still_matches(self, copy: LibraryCopy) -> bool:
        try:
            return self._fsops.sha256_of(copy.local) == copy.file_hash
        except (FileNotFoundError, FsopsError):
            return False

    def _check_received(
        self, received: Received, expected_payload: str, rendered: set[str]
    ) -> None:
        if received.sha256 in rendered:  # same payload, but it is a destination's rendering
            raise SafeguardFailedError("rendered_file", "the origin returned a rendered file")
        try:
            if _payload_hash(received.path, "x.cbz") != expected_payload:
                raise SafeguardFailedError(
                    "content_hash_mismatch", "the origin holds another revision"
                )
        except (UnsafeArchiveError, UntrustedEntryError) as error:
            raise SafeguardFailedError("origin_refused", str(error)) from error

    def _place(self, local: str, source: Path | BinaryIO, expected_payload: str) -> str:
        """Verify the payload about to enter /store, then place it. Returns its sha256."""
        if isinstance(source, Path):
            file_hash = sha256_file(source)
            with source.open("rb") as stream:
                self._verify_and_write(local, stream, expected_payload, file_hash)
            return file_hash
        digest = _hash_stream(source)
        self._verify_and_write(local, source, expected_payload, digest)
        return digest

    def _verify_and_write(
        self, local: str, stream: BinaryIO, expected_payload: str, file_hash: str
    ) -> None:
        stream.seek(0)
        try:
            if _payload_hash(stream, "x.cbz") != expected_payload:
                raise SafeguardFailedError("content_hash_mismatch", "the payload does not verify")
        except (UnsafeArchiveError, UntrustedEntryError) as error:
            raise SafeguardFailedError("content_hash_mismatch", str(error)) from error
        stream.seek(0)
        self._fsops.write_atomic(
            local, stream, parents=True, replace=True,
            verify_digest=lambda: file_hash.removeprefix("sha256:"),
        )  # fmt: skip

    def _record(
        self,
        unit_id: str,
        content_id: str,
        rel: str,
        local: str,
        provenance: str,
        file_hash: str,
        payload_hash: str,
    ) -> None:
        root_id, root_rel = self._locate(local)
        observed = self._fsops.observe(local)
        if observed is None:
            raise SafeguardFailedError("vanished", "the stored file disappeared after writing")
        identity = observed[1]
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(storage.content)
                .where(storage.content.c.id == content_id)
                .values(retention="keep", path=rel, store_provenance=provenance)
            )
            record_replica(
                connection, unit_id=unit_id, location="store", content_hash=payload_hash,
                file_hash=file_hash, independent=True, physical_root_id=root_id,
                rel_path=root_rel, dev=self._fsops.device_of(local), ino=identity.ino,
                size=identity.size, mtime_ns=identity.mtime_ns, verified_at=datetime.now(UTC),
            )  # fmt: skip


def _hash_stream(stream: BinaryIO) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    while chunk := stream.read(_CHUNK):
        digest.update(chunk)
    return "sha256:" + digest.hexdigest()
