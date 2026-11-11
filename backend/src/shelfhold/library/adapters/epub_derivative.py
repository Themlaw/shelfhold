"""The pipeline of an EPUB from a source that is not trusted (EF-BIB-051, 02 section 5.8).

What happens to an `untrusted` EPUB, in this order and nowhere else:

1. `neutralize_epub` writes the **derivative** to a staged file. If it refuses the EPUB there is
   no derivative, nothing is written, the Unit is `quarantined` with the reason and the way out is
   a replacement source (a later ingest that passes);
2. the original is put in `/store/untrusted/` (an atomic write, read back), the only file of
   `/store` in passthrough mode; without a `/store` the warning `content.original_not_kept` is
   given and only the derivative is kept (degraded mode);
3. the derivative is put in `/library`, read back, and one transaction records the content
   (`original_sha256`, `content_hash` of the original, `derived_hash`, `derived_by`,
   `derived_hash_by`), the copy in `/store` (`fidelity = original`) and the one in `/library`
   (`fidelity = derived`).

**What leaves Shelfhold is `servable()`'s file and nothing else.** It is the derivative recorded
for the content, verified now (the hash of its payload is `derived_hash`, and the neutralizer
finds nothing left to do). A file in `/library` that is the original (put back by a restore, an
adoption or a repair), a file of `/store`, a content with no derivative: `OriginalNotServableError`.

Regeneration after a security update of the neutralizer restarts from the original in `/store` if
it still verifies by its hash, else from the derivative in `/library` if it still verifies by
`derived_hash`; the generation replaced goes to `derived_chain`. The new file is verified before it
replaces anything, by an atomic replacement (never a deletion first), and a failure at any step
leaves the old file and the old rows.
"""

import functools
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any, BinaryIO, Final, Literal

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.archives import (
    DEFAULT_LIMITS,
    ArchiveLimits,
    UnsafeArchiveError,
    safe_open_archive,
)
from shelfhold.infrastructure.content_trust import (
    DERIVED_BY,
    DERIVED_HASH_BY,
    NeutralizationError,
    epub_payload_hash,
    find_active_content,
    neutralize_epub,
)
from shelfhold.infrastructure.fsops import Fsops, FsopsError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, storage
from shelfhold.library.adapters.meta_projection import MetaProjection
from shelfhold.library.adapters.storage import (
    DEFAULT_MAX_RECEIVED_BYTES,
    ContentTombstones,
    Ingested,
    RootLocator,
    sha256_file,
)
from shelfhold.library.application.reneutralize import Neutralize
from shelfhold.library.domain.content_hash import FILE_HASH_BY
from shelfhold.library.domain.copies import CopyFacts, is_independent
from shelfhold.library.domain.failures import (
    EpubRefusedError,
    OriginalNotServableError,
    ReceivedTooLargeError,
    RegenerationBlockedError,
    TombstonedContentError,
    VariantAlreadyStoredError,
)
from shelfhold.library.domain.untrusted import (
    EPUB_MEDIA_TYPE,
    ORIGINAL_NOT_KEPT,
    UNTRUSTED,
    binding_warning,
    chain_entry,
    extend_chain,
    original_rel,
    trusted_as_published,
)
from shelfhold.library.replica import (
    library_copies,
    record_library_rewrite,
    record_replica,
    sibling_inodes,
    store_copies,
)
from shelfhold.library.tombstones.values import ContentHash

log = structlog.get_logger(__name__)

_EPUB_HASH_BY: Final = DERIVED_HASH_BY


@dataclass(frozen=True)
class EpubIngested:
    content_id: str
    replica_id: str
    original_sha256: str
    derived_hash: str | None  # None for an EPUB filed as published
    size: int
    original_kept: bool
    degraded: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServableEpub:
    """The one file that may leave Shelfhold for this Unit."""

    path: str
    content_id: str
    derived_hash: str | None
    file_hash: str


@dataclass(frozen=True)
class Regenerated:
    unit_id: str
    content_id: str
    source: Literal["original", "derived"]
    derived_by: str
    derived_hash: str


def _unit_content(connection: Connection, unit_id: str) -> Any:
    content, variant = storage.content, catalog.unit_variant
    return connection.execute(
        sa.select(content)
        .select_from(content.outerjoin(variant, variant.c.id == content.c.variant_id))
        .where(content.c.unit_id == unit_id, content.c.format == "epub")
        .order_by(sa.func.coalesce(variant.c.preferred, False).desc(), content.c.id)
    ).first()


def trust_of(connection: Connection, variant_id: str | None) -> str:
    """`content_trust` of the binding a Variant came by; `untrusted` when it cannot be read."""
    if variant_id is None:
        return "untrusted"
    value = connection.execute(
        sa.select(acquisition.source_binding.c.content_trust)
        .select_from(
            catalog.unit_variant.join(
                acquisition.source_binding,
                acquisition.source_binding.c.id == catalog.unit_variant.c.source_binding_id,
            )
        )
        .where(catalog.unit_variant.c.id == variant_id)
    ).scalar()
    return "publisher" if value == "publisher" else "untrusted"


def require_servable_derivative(connection: Connection, unit_id: str) -> None:
    """The database half of the rule, for an export that only registers (a `pull` destination
    serves bytes later, from `servable()`): an `untrusted` EPUB with no derivative is not
    exported. A Unit with no EPUB is not concerned."""
    content = _unit_content(connection, unit_id)
    if content is None or content.derived_hash is not None:
        return
    if trusted_as_published(trust_of(connection, content.variant_id)):
        return
    raise OriginalNotServableError(
        "no_derivative", "an untrusted EPUB is exported by its derivative"
    )


def has_active_content(fsops: Fsops, local: str) -> bool:
    """Does this EPUB hold active content? For the adoption report: an EPUB already in a library
    has no source to vouch for it, so it is read as `untrusted` (the default) and left exactly as it
    is. A file that cannot be read at all is not reported (nothing is known)."""
    try:
        with fsops.open_read(local) as stream:
            return bool(find_active_content(stream, name=PurePath(local).name))
    except (OSError, FsopsError):
        return False


def _payload_of(stream: BinaryIO) -> str:
    with safe_open_archive(stream, name="x.epub") as archive:
        return epub_payload_hash(archive)


def _is_clean(
    source: Path | BinaryIO, *, staging: Path | None = None, limits: ArchiveLimits = DEFAULT_LIMITS
) -> bool:
    """The neutralizer, the shipped release, has nothing left to change in this EPUB."""
    if not isinstance(source, Path):
        source.seek(0)
    with tempfile.TemporaryFile(dir=staging) as sink:
        try:
            made = neutralize_epub(source, sink, name="x.epub", limits=limits)
        except NeutralizationError:
            return False
    return not made.changed


def has_epub(connection: Connection, unit_id: str) -> bool:
    return _unit_content(connection, unit_id) is not None


def servable_epub(
    engine: Engine, fsops: Fsops, unit_id: str, *, staging: Path | None = None
) -> ServableEpub:
    """The one file that may be served or exported for the Unit's EPUB: its verified derivative
    (or, for a `publisher` EPUB, the file as filed). Never a file of /store, never an original:
    a library file that is not the recorded derivative, or that the neutralizer would still change,
    serves nothing (a restore, an adoption or a repair may have put the original there)."""
    with engine.connect() as connection:
        content = _unit_content(connection, unit_id)
        if content is None:
            raise OriginalNotServableError("no_epub")
        trust = trust_of(connection, content.variant_id)
        copies = list(library_copies(connection, unit_id))
    published = trusted_as_published(trust) and content.derived_hash is None
    if content.derived_hash is None and not published:
        raise OriginalNotServableError("no_derivative")
    if content.retention == "adopted_ref" and not published:
        raise OriginalNotServableError("adopted_in_place", "the file is the original")
    for copy in copies:
        try:
            with fsops.open_read(copy.local) as stream:
                if published or (
                    _payload_of(stream) == content.derived_hash
                    and _is_clean(stream, staging=staging)
                ):
                    return ServableEpub(
                        copy.local, content.id, content.derived_hash, fsops.sha256_of(copy.local)
                    )
        except (FileNotFoundError, FsopsError, UnsafeArchiveError):
            continue
    raise OriginalNotServableError("library_not_derivative")


# The lock of one Work (`work:<id>`), the one `purge --now` holds while it erases (EF-BIB-052).
# It is a required argument with no default: no write under /store or `received/` without it.
WorkHolder = Callable[[str], AbstractContextManager[Any]]


def _under_work[F: Callable[..., Any]](method: F) -> F:
    """Run a writing method with the lock of the Unit's Work held, from its first read to its last
    write: a purge that holds it first leaves nothing to write; one that waits finds the files and
    the rows complete."""

    @functools.wraps(method)
    def locked(self: "UntrustedEpubStore", *args: Any, **kwargs: Any) -> Any:
        unit_id = kwargs["unit_id"] if "unit_id" in kwargs else args[0]
        with self._hold_work(self._work_of(unit_id)):
            return method(self, *args, **kwargs)

    return locked  # type: ignore[return-value]


class UntrustedEpubStore:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        locate: RootLocator,
        data_dir: Path,
        *,
        store_dir: str | None,
        tombstones: ContentTombstones,
        hold_work: WorkHolder,
        max_received_bytes: int = DEFAULT_MAX_RECEIVED_BYTES,
        limits: ArchiveLimits = DEFAULT_LIMITS,
        neutralize: Neutralize = neutralize_epub,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._locate = locate
        self._data_dir = data_dir
        self._store = store_dir.rstrip("/") if store_dir else None
        self._tombstones = tombstones
        self._hold_work = hold_work
        self._max = max_received_bytes
        self._limits = limits
        self._neutralize = neutralize
        self._projection = MetaProjection(engine, data_dir)

    # -- ingest -----------------------------------------------------------------------------

    @_under_work
    def ingest(
        self,
        *,
        unit_id: str,
        variant_id: str,
        staged: Path,
        dest: str,
        expected_sha256: str | None = None,
        upstream_ref: str | None = None,
    ) -> EpubIngested:
        """File the EPUB `staged` at `dest` in /library: its derivative if the source is not
        trusted. Raises, having changed nothing but the refusal mark, for a file too large, not
        what was announced, tombstoned, already there, or refused by the neutralizer."""
        if staged.stat().st_size > self._max:
            raise ReceivedTooLargeError(f"{staged.stat().st_size} bytes, above {self._max}")
        original = sha256_file(staged)
        if expected_sha256 is not None and expected_sha256 != original:
            raise ValueError("received_hash_mismatch: the file is not what the download counted")
        with self._engine.connect() as connection:
            if connection.execute(
                sa.select(storage.content.c.id).where(
                    storage.content.c.variant_id == variant_id,
                    storage.content.c.media_type == EPUB_MEDIA_TYPE,
                )
            ).first():
                raise VariantAlreadyStoredError(f"the Variant {variant_id} already has its EPUB")
            trust = trust_of(connection, variant_id)
        if trusted_as_published(trust):
            return self._file_as_published(
                unit_id, variant_id, staged, dest, original, upstream_ref
            )
        with tempfile.NamedTemporaryFile(
            dir=self._staging(), suffix=".epub", prefix="derived-"
        ) as out:
            try:
                made = self._neutralize(staged, out, name=PurePath(dest).name, limits=self._limits)
            except NeutralizationError as error:
                self._refuse(unit_id, variant_id, error)
                raise EpubRefusedError(error.reason, error.entry) from None
            out.flush()
            derived_path = Path(out.name)
            derived_sha = sha256_file(derived_path)
            self._verify_derivative(derived_path, made.derived_hash)
            self._require_not_tombstoned(
                [
                    ContentHash(_EPUB_HASH_BY, made.original_hash),
                    ContentHash(_EPUB_HASH_BY, made.derived_hash),
                    ContentHash(FILE_HASH_BY, original),
                ]
            )
            return self._place(
                unit_id,
                variant_id,
                staged,
                dest,
                original,
                derived_path,
                derived_sha,
                made,
                upstream_ref,
            )

    def _place(
        self, unit_id: str, variant_id: str, staged: Path, dest: str, original: str,
        derived_path: Path, derived_sha: str, made: Any, upstream_ref: str | None,
    ) -> EpubIngested:  # fmt: skip
        content_id = new_ulid()
        kept_local: str | None = None
        rel = original_rel(unit_id, content_id)
        if self._store is not None:  # the original first: nothing is shown before it is safe
            kept_local = f"{self._store}/{rel}"
            with staged.open("rb") as stream:
                self._fsops.write_atomic(
                    kept_local, stream, parents=True, replace=False,
                    verify_digest=lambda: original.removeprefix("sha256:"),
                )  # fmt: skip
        root_id, library_rel = self._locate(dest)
        with derived_path.open("rb") as stream:
            identity = self._fsops.write_atomic(
                dest, stream, parents=True, replace=False,
                verify_digest=lambda: derived_sha.removeprefix("sha256:"),
            )  # fmt: skip
        device, now = self._fsops.device_of(dest), datetime.now(UTC)
        kept = kept_local is not None
        with self._engine.begin() as connection:
            connection.execute(
                sa.insert(storage.content).values(
                    id=content_id, unit_id=unit_id, variant_id=variant_id, kind="text",
                    media_type=EPUB_MEDIA_TYPE, format="epub",
                    retention="keep" if kept else "passthrough", path=rel if kept else None,
                    original_sha256=original, content_hash=made.original_hash,
                    content_hash_by=_EPUB_HASH_BY, store_provenance="received",
                    derived_hash=made.derived_hash, derived_by=made.derived_by,
                    derived_hash_by=made.derived_hash_by, size=identity.size,
                )
            )  # fmt: skip
            replica_id = record_replica(
                connection, unit_id=unit_id, location="library", content_hash=made.derived_hash,
                file_hash=derived_sha, independent=True, mutable_by_third_party=True,
                physical_root_id=root_id, rel_path=library_rel, dev=device, ino=identity.ino,
                size=identity.size, mtime_ns=identity.mtime_ns, fidelity="derived", verified_at=now,
            )  # fmt: skip
            if kept_local is not None:
                self._record_kept(
                    connection, unit_id, kept_local, original, made.original_hash, now
                )
            if upstream_ref is not None:
                record_replica(
                    connection, unit_id=unit_id, location="upstream",
                    content_hash=made.original_hash, file_hash=original, independent=True,
                    mutable_by_third_party=True, remote_ref=upstream_ref, size=identity.size,
                    fidelity="original", verified_at=now,
                )  # fmt: skip
            self._leave_refused_state(connection, unit_id)
        self._projection.project(unit_id)
        warnings: tuple[str, ...] = ()
        if not kept:
            warning = binding_warning(UNTRUSTED, store_available=False)
            warnings = (warning,) if warning else ()
            log.warning(ORIGINAL_NOT_KEPT, content_id=content_id)
        return EpubIngested(
            content_id, replica_id, original, made.derived_hash, identity.size, kept,
            degraded=not kept, warnings=warnings,
        )  # fmt: skip

    def _record_kept(
        self, connection: Connection, unit_id: str, local: str, original: str, payload: str,
        now: datetime,
    ) -> None:  # fmt: skip
        found = self._fsops.observe(local)
        if found is None:
            raise FileNotFoundError(local)
        device, seen = self._fsops.device_of(local), self._fsops.sha256_of(local)
        siblings = sibling_inodes(connection, unit_id)
        root_id, root_rel = self._locate(local)
        record_replica(
            connection, unit_id=unit_id, location="store", content_hash=payload, file_hash=seen,
            independent=is_independent(
                CopyFacts(device, found[1].ino, seen), stored_sha256=original, siblings=siblings
            ),
            physical_root_id=root_id, rel_path=root_rel, dev=device, ino=found[1].ino,
            size=found[1].size, mtime_ns=found[1].mtime_ns, fidelity="original", verified_at=now,
        )  # fmt: skip

    def _file_as_published(
        self, unit_id: str, variant_id: str, staged: Path, dest: str, original: str,
        upstream_ref: str | None,
    ) -> EpubIngested:  # fmt: skip
        """`publisher`: the file as received, no derivative (ADR-0033: never rewritten)."""
        with safe_open_archive(staged, name=PurePath(dest).name, limits=self._limits) as archive:
            payload = epub_payload_hash(archive)
        self._require_not_tombstoned(
            [ContentHash(_EPUB_HASH_BY, payload), ContentHash(FILE_HASH_BY, original)]
        )
        root_id, rel = self._locate(dest)
        with staged.open("rb") as stream:
            identity = self._fsops.write_atomic(
                dest, stream, parents=True, replace=False,
                verify_digest=lambda: original.removeprefix("sha256:"),
            )  # fmt: skip
        device, now, content_id = self._fsops.device_of(dest), datetime.now(UTC), new_ulid()
        with self._engine.begin() as connection:
            connection.execute(
                sa.insert(storage.content).values(
                    id=content_id, unit_id=unit_id, variant_id=variant_id, kind="text",
                    media_type=EPUB_MEDIA_TYPE, format="epub", retention="passthrough",
                    original_sha256=original, content_hash=payload,
                    content_hash_by=_EPUB_HASH_BY, store_provenance="received", size=identity.size,
                )
            )  # fmt: skip
            replica_id = record_replica(
                connection, unit_id=unit_id, location="library", content_hash=payload,
                file_hash=original, independent=True, mutable_by_third_party=True,
                physical_root_id=root_id, rel_path=rel, dev=device, ino=identity.ino,
                size=identity.size, mtime_ns=identity.mtime_ns, verified_at=now,
            )  # fmt: skip
            if upstream_ref is not None:
                record_replica(
                    connection, unit_id=unit_id, location="upstream", content_hash=payload,
                    file_hash=original, independent=True, mutable_by_third_party=True,
                    remote_ref=upstream_ref, size=identity.size, verified_at=now,
                )  # fmt: skip
        self._projection.project(unit_id)
        return EpubIngested(content_id, replica_id, original, None, identity.size, False)

    # -- the refused state --------------------------------------------------------------------

    def _refuse(self, unit_id: str, variant_id: str, error: NeutralizationError) -> None:
        """Visible, with its reason: the Unit is `quarantined`, the revision says why."""
        reason = f"{error.code}: {error.reason}" + (f" ({error.entry})" if error.entry else "")
        now = datetime.now(UTC)
        revision = catalog.variant_revision
        with self._engine.begin() as connection:
            latest = connection.execute(
                sa.select(revision.c.id, revision.c.quarantined_at)
                .where(revision.c.variant_id == variant_id)
                .order_by(revision.c.revision.desc())
            ).first()
            if latest is None:
                connection.execute(
                    sa.insert(revision).values(
                        variant_id=variant_id,
                        revision=1,
                        seen_at=now,
                        quarantined_at=now,
                        quarantine_reason=reason,
                    )
                )
            elif latest.quarantined_at is None:  # set once, never lifted
                connection.execute(
                    sa.update(revision)
                    .where(revision.c.id == latest.id)
                    .values(quarantined_at=now, quarantine_reason=reason)
                )
            connection.execute(
                sa.update(catalog.unit)
                .where(catalog.unit.c.id == unit_id)
                .values(state="quarantined")
            )
        log.warning("content.untrusted_entry", reason=error.reason, entry=error.entry)

    @staticmethod
    def _leave_refused_state(connection: Connection, unit_id: str) -> None:
        connection.execute(
            sa.update(catalog.unit)
            .where(catalog.unit.c.id == unit_id, catalog.unit.c.state == "quarantined")
            .values(state="stored")
        )

    # -- restoring the derivative from a destination ------------------------------------------

    @_under_work
    def restore_from_destination(
        self, *, unit_id: str, variant_id: str, found: Path, dest: str,
        expected_derived_hash: str, derived_by: str,
    ) -> EpubIngested:  # fmt: skip
        """The original is lost and a destination holds the derivative: take it back. The file is
        verified by `derived_hash` (the payload, so the OPF a destination rewrote does not count)
        and must be clean for the neutralizer; it is recorded `extracted_derived` and nothing is
        put in /store, for there is no original to keep."""
        with safe_open_archive(found, name=PurePath(dest).name, limits=self._limits) as archive:
            payload = epub_payload_hash(archive)
        if payload != expected_derived_hash:
            raise OriginalNotServableError("derived_hash_mismatch", "not the derivative recorded")
        if not _is_clean(found, staging=self._staging(), limits=self._limits):
            raise OriginalNotServableError("not_clean", "the neutralizer would still change it")
        self._require_not_tombstoned([ContentHash(_EPUB_HASH_BY, payload)])
        file_hash, content_id = sha256_file(found), new_ulid()
        root_id, rel = self._locate(dest)
        with found.open("rb") as stream:
            identity = self._fsops.write_atomic(
                dest, stream, parents=True, replace=False,
                verify_digest=lambda: file_hash.removeprefix("sha256:"),
            )  # fmt: skip
        device, now = self._fsops.device_of(dest), datetime.now(UTC)
        with self._engine.begin() as connection:
            connection.execute(
                sa.insert(storage.content).values(
                    id=content_id, unit_id=unit_id, variant_id=variant_id, kind="text",
                    media_type=EPUB_MEDIA_TYPE, format="epub", retention="passthrough",
                    original_sha256=file_hash, content_hash=None,
                    store_provenance="extracted_derived", derived_hash=payload,
                    derived_by=derived_by, derived_hash_by=_EPUB_HASH_BY, size=identity.size,
                )
            )  # fmt: skip
            replica_id = record_replica(
                connection, unit_id=unit_id, location="library", content_hash=payload,
                file_hash=file_hash, independent=True, mutable_by_third_party=True,
                physical_root_id=root_id, rel_path=rel, dev=device, ino=identity.ino,
                size=identity.size, mtime_ns=identity.mtime_ns, fidelity="derived", verified_at=now,
            )  # fmt: skip
            self._leave_refused_state(connection, unit_id)
        self._projection.project(unit_id)
        return EpubIngested(
            content_id, replica_id, file_hash, payload, identity.size, False, degraded=True
        )

    # -- what may leave ---------------------------------------------------------------------

    # -- regeneration (security update of the neutralizer) ---------------------------------

    def source_of(self, unit_id: str) -> tuple[Literal["original", "derived", "blocked"], str]:
        """Where a regeneration would restart from, and why not when it cannot."""
        with self._engine.connect() as connection:
            content = _unit_content(connection, unit_id)
            stores = list(store_copies(connection, unit_id))
            copies = list(library_copies(connection, unit_id))
        if content is None or content.derived_hash is None:
            return "blocked", "no_derivative"
        for kept in stores:
            if "/untrusted/" not in kept.local:
                continue  # only the originals of this pipeline, under /store/untrusted/
            with suppress(FileNotFoundError, FsopsError):
                if self._fsops.sha256_of(kept.local) == content.original_sha256:
                    return "original", kept.local
        for copy in copies:
            with (
                suppress(FileNotFoundError, FsopsError, UnsafeArchiveError),
                self._fsops.open_read(copy.local) as stream,
            ):
                if _payload_of(stream) == content.derived_hash:
                    return "derived", copy.local
        return "blocked", "neither_original_nor_derivative_verifies"

    @_under_work
    def regenerate(
        self, unit_id: str, *, neutralize: Neutralize | None = None, target: str = DERIVED_BY
    ) -> Regenerated:
        """Make the derivative again: the same call is a security update and a repair."""
        run = neutralize or self._neutralize
        source, where = self.source_of(unit_id)
        if source == "blocked":
            raise RegenerationBlockedError(where)
        with self._engine.connect() as connection:
            content = _unit_content(connection, unit_id)
            copies = list(library_copies(connection, unit_id))
        if content is None or not copies:
            raise RegenerationBlockedError("no_library_copy")
        into = copies[0]
        with tempfile.NamedTemporaryFile(
            dir=self._staging(), suffix=".epub", prefix="regen-"
        ) as out:
            with self._fsops.open_read(where) as stream:
                made = run(stream, out, name="book.epub", limits=self._limits)
            out.flush()
            if made.derived_by != target:
                raise RegenerationBlockedError("derived_by_is_not_the_target")
            staged = Path(out.name)
            self._verify_derivative(staged, made.derived_hash)  # verified before it replaces
            digest = sha256_file(staged)
            now = datetime.now(UTC)
            with staged.open("rb") as stream:
                written = self._fsops.write_atomic(
                    into.local, stream, replace=True,
                    verify_digest=lambda: digest.removeprefix("sha256:"),
                )  # fmt: skip
            with self._engine.begin() as connection:
                record_library_rewrite(
                    connection, into.replica_id, file_hash=digest,
                    dev=self._fsops.device_of(into.local), ino=written.ino, size=written.size,
                    mtime_ns=written.mtime_ns, verified_at=now, content_hash=made.derived_hash,
                )  # fmt: skip
                connection.execute(
                    sa.update(storage.content)
                    .where(storage.content.c.id == content.id)
                    .values(
                        derived_hash=made.derived_hash,
                        derived_by=made.derived_by,
                        derived_hash_by=made.derived_hash_by,
                        size=written.size,
                        derived_chain=extend_chain(
                            content.derived_chain,
                            chain_entry(
                                content.derived_by,
                                content.derived_hash,
                                content.derived_hash_by,
                                now,
                            ),
                        ),
                    )
                )
        self._projection.project(unit_id)
        return Regenerated(unit_id, content.id, source, made.derived_by, made.derived_hash)

    # -- internals --------------------------------------------------------------------------

    def _work_of(self, unit_id: str) -> str:
        with self._engine.connect() as connection:
            work = connection.execute(
                sa.select(catalog.edition.c.work_id)
                .select_from(catalog.unit.join(catalog.edition))
                .where(catalog.unit.c.id == unit_id)
            ).scalar()
        if work is None:
            raise RegenerationBlockedError("unknown_unit")
        return str(work)

    def _staging(self) -> Path:
        staging = self._data_dir / "tmp"
        return staging if staging.is_dir() else self._data_dir

    def _verify_derivative(self, path: Path, expected: str) -> None:
        with safe_open_archive(path, name="derived.epub", limits=self._limits) as archive:
            if epub_payload_hash(archive) != expected:
                raise RegenerationBlockedError("derived_hash_does_not_verify")

    def _require_not_tombstoned(self, hashes: list[ContentHash]) -> None:
        if self._tombstones.check_content(hashes):
            raise TombstonedContentError("the content was purged: it is not filed again")


__all__ = [
    "EpubIngested",
    "Ingested",
    "Regenerated",
    "ServableEpub",
    "UntrustedEpubStore",
    "binding_warning",
    "has_active_content",
    "has_epub",
    "require_servable_derivative",
    "servable_epub",
    "trust_of",
]
