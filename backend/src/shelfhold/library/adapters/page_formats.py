"""Pages in formats every reader takes, from the day an archive comes in (EF-EXP-046, ADR-0054).

The canonical copy is the archive as received. With `/store` configured it is kept there byte for
byte and `/library` holds the normalized derivative (`derived_by = page-normalizer@v1`), declared
through `KeepCopier.declare_derivative`: the original is copied first, under its intent, then the
library file is replaced atomically and read back; the two count as independent copies. Without
`/store` nothing is rewritten in place (a lossy rewrite with no original cannot be undone): the
archive stays as received and is normalized on the way out, by the exporter.

`PageFormatCbzStore` is the door of every road that files a CBZ (import folder, sources, Suwayomi,
adoption). `LibraryPageNormalizer` is the same step applied to what is already filed
(`shelfhold library normalize-pages`): it is stateless, so a run cut off by a kill -9 is finished
by running it again.

A page that cannot be read (empty, undecodable, too large, a format with no decoder) is reported
with a closed code (`page.*`) and the archive is filed or left as received: never half converted.
"""

from __future__ import annotations

import errno
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Final, cast

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.infrastructure.archives import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.content_trust import UntrustedEntryError
from shelfhold.infrastructure.fsops import Fsops, FsopsError
from shelfhold.infrastructure.imaging import ImageRejected
from shelfhold.infrastructure.imaging.errors import (
    PAGES_NOT_KEPT,
    PAGES_STORE_NOT_CONFIGURED,
    PAGES_UNREADABLE,
    PAGES_WORK_BUSY,
    PAGES_WORKER_FAILED,
    WORKER_FAILED,
)
from shelfhold.infrastructure.packaging.cbz import (
    NormalizeReport,
    PageIssue,
    PageTarget,
    needs_normalization,
    normalize_cbz,
)
from shelfhold.infrastructure.schema import storage
from shelfhold.library.adapters.storage import (
    CBZ_MEDIA_TYPE,
    KeepCopier,
    PassthroughStore,
    WorkHolder,
    keep_rel,
    sha256_file,
    work_of_unit,
)
from shelfhold.library.adapters.svg_derivative import SvgCbzStore
from shelfhold.library.domain.failures import SafeguardFailedError, VariantAlreadyStoredError
from shelfhold.library.page_issues import clear_page_issue, note_page_issue, page_issue_for
from shelfhold.library.replica import library_copies

__all__ = [
    "PAGE_DERIVED_BY",
    "STORE_NOT_CONFIGURED",
    "LibraryPageNormalizer",
    "PageFormatCbzStore",
    "PageNormalizeSummary",
    "PageStoreNotConfiguredError",
    "PendingUnit",
    "UnitOutcome",
]

log = structlog.get_logger(__name__)
PAGE_DERIVED_BY: Final = "page-normalizer@v1"
STORE_NOT_CONFIGURED: Final = PAGES_STORE_NOT_CONFIGURED
IssuesHook = Callable[[str, tuple[PageIssue, ...]], None]


class PageStoreNotConfiguredError(RuntimeError):
    """The library is not rewritten without `/store`: the received file would be lost."""

    code = STORE_NOT_CONFIGURED


@contextmanager
def _derivative_of(
    source: Path, workdir: Path, target: PageTarget
) -> Iterator[tuple[Path, NormalizeReport]]:
    """The normalized archive in a staging file for the `with` block, and the report. The file is
    gone after it; it only means something when `report.status == "normalized"`."""
    with tempfile.NamedTemporaryFile(prefix="pages-", suffix=".cbz", dir=workdir) as staging:
        with safe_open_archive(source, name="original.cbz") as archive:
            report = normalize_cbz(archive, cast(BinaryIO, staging), target=target)
        staging.flush()
        yield Path(staging.name), report


def _wants_normalizing(source: Path) -> bool:
    try:
        with safe_open_archive(source, name="received.cbz") as archive:
            return needs_normalization(archive)
    except (UnsafeArchiveError, UntrustedEntryError, OSError):
        return False  # the plain store refuses it, with its own error


class PageFormatCbzStore:
    """`SvgCbzStore.ingest`, plus the normalized derivative of an archive whose pages are not all
    in allowlisted formats. Same surface (`ingest`, `derivative_missing`, `finish_derivative`)."""

    def __init__(
        self,
        inner: SvgCbzStore,
        store: PassthroughStore,
        keeper: KeepCopier,
        workdir: Path,
        engine: Engine,
        hold_work: WorkHolder,
        *,
        target: PageTarget | None = None,
        keeps_originals: bool = True,
        on_issues: IssuesHook | None = None,
    ) -> None:
        self._inner = inner
        self._store = store
        self._keeper = keeper
        self._workdir = workdir
        self._engine = engine
        self._hold_work = hold_work
        self._target = target or PageTarget()
        self._keeps = keeps_originals
        self._on_issues = on_issues

    def _held(self, unit_id: str) -> AbstractContextManager[Any]:
        with self._engine.connect() as connection:
            work_id = work_of_unit(connection, unit_id)
        return nullcontext() if work_id is None else self._hold_work(work_id)

    def _report(
        self, unit_id: str, issues: tuple[PageIssue, ...], original_sha256: str | None = None
    ) -> None:
        for issue in issues:
            log.info("pages.not_normalized", unit_id=unit_id, code=issue.code)
        if issues and original_sha256 is not None:  # the chapter is set aside and shown on Health
            note_page_issue(self._engine, unit_id, original_sha256, issues[0].code)
        if self._on_issues is not None:
            self._on_issues(unit_id, issues)

    def ingest(self, **arguments: Any) -> Any:
        staged: Path = arguments["staged"]
        unit_id: str = arguments["unit_id"]
        if SvgCbzStore._has_svg(staged) or not _wants_normalizing(staged):
            return self._inner.ingest(**arguments)
        if not self._keeps:
            log.info("pages.normalization_deferred", unit_id=unit_id, reason="no_store")
            return self._inner.ingest(**arguments)
        try:
            with _derivative_of(staged, self._workdir, self._target) as (derived, report):
                if report.status != "normalized":
                    ingested = self._inner.ingest(**arguments)  # as received
                    self._report(unit_id, report.issues, sha256_file(staged))
                    return ingested
                ingested = self._file(arguments, unit_id, derived)
                clear_page_issue(self._engine, unit_id)
                return ingested
        except UnsafeArchiveError:
            return self._inner.ingest(**arguments)  # the plain store says why
        except ImageRejected as error:
            if error.code != WORKER_FAILED:
                raise
            # a page that kills the decoder is not tried again at every cycle: the chapter is
            # filed as received, set aside, and shown on Health (`pages.worker_failed`)
            ingested = self._inner.ingest(**arguments)
            note_page_issue(
                self._engine, unit_id, sha256_file(staged), PAGES_WORKER_FAILED, died=True
            )
            log.warning("pages.worker_failed", unit_id=unit_id)
            return ingested

    def _file(self, arguments: dict[str, Any], unit_id: str, derived: Path) -> Any:
        """The original filed as received, then its derivative declared, under one hold."""
        try:
            with self._held(unit_id):
                try:
                    ingested = self._store.ingest(**arguments, allow_convertible_pages=True)
                except VariantAlreadyStoredError:  # a replay: finish what the first pass left
                    if self.derivative_missing(unit_id, arguments["staged"]):
                        self._declare(unit_id, derived)
                    raise
                self._declare(unit_id, derived)
                return ingested
        except SafeguardFailedError as error:  # the original is filed; the replay finishes it
            raise OSError(errno.EAGAIN, f"page_derivative_pending: {error.reason}") from error

    def _declare(self, unit_id: str, derived: Path) -> None:
        self._keeper.declare_derivative(unit_id, derived, PAGE_DERIVED_BY, lossy=True)

    def derivative_missing(self, unit_id: str, staged: Path) -> bool:
        """Does `staged` need normalizing while its Unit's content has no derivative yet?"""
        if not self._keeps or SvgCbzStore._has_svg(staged) or not _wants_normalizing(staged):
            return self._inner.derivative_missing(unit_id, staged)
        with self._engine.connect() as connection:
            hashes = (
                connection.execute(
                    sa.select(storage.content.c.derived_hash).where(
                        storage.content.c.unit_id == unit_id,
                        storage.content.c.original_sha256 == sha256_file(staged),
                    )
                )
                .scalars()
                .all()
            )
        return not hashes or any(h is None for h in hashes)

    def finish_derivative(self, *, unit_id: str, staged: Path) -> None:
        """Declare the derivative the Unit still lacks (`OSError` EAGAIN while it cannot)."""
        if SvgCbzStore._has_svg(staged) or not _wants_normalizing(staged) or not self._keeps:
            self._inner.finish_derivative(unit_id=unit_id, staged=staged)
            return
        try:
            with self._held(unit_id):
                if not self.derivative_missing(unit_id, staged):
                    return
                with _derivative_of(staged, self._workdir, self._target) as (derived, report):
                    if report.status == "normalized":
                        self._declare(unit_id, derived)
                        clear_page_issue(self._engine, unit_id)
                    else:
                        self._report(unit_id, report.issues, sha256_file(staged))
        except SafeguardFailedError as error:
            raise OSError(errno.EAGAIN, f"page_derivative_pending: {error.reason}") from error


# -- what is already filed -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingUnit:
    unit_id: str
    content_id: str
    local: str
    original_sha256: str = ""
    resume: bool = False  # the library file is already the derivative: only the books are left


@dataclass(frozen=True, slots=True)
class UnitOutcome:
    unit_id: str
    status: str  # `normalized`, `kept_as_received`, `unchanged` or `failed`
    issues: tuple[PageIssue, ...] = ()
    code: str | None = None  # a closed code for `failed`


@dataclass(slots=True)
class PageNormalizeSummary:
    dry_run: bool
    pending: list[PendingUnit] = field(default_factory=list)
    reports: list[UnitOutcome] = field(default_factory=list)

    def _count(self, status: str) -> int:
        return sum(1 for r in self.reports if r.status == status)

    @property
    def normalized(self) -> int:
        return self._count("normalized")

    @property
    def kept_as_received(self) -> int:
        return self._count("kept_as_received")

    @property
    def failed(self) -> int:
        return self._count("failed")


class LibraryPageNormalizer:
    """`library normalize-pages`: the library's Units whose archive still holds a page in a format
    to convert get their derivative, original kept in /store first. Stateless: what a kill -9 cut
    off is finished by the next run (`KeepCopier.declare_derivative` is resumable)."""

    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        locate: Any,
        keeper: KeepCopier,
        workdir: Path,
        hold_work: WorkHolder,
        *,
        target: PageTarget | None = None,
        store_configured: bool = True,
        store_dir: str | None = None,
    ) -> None:
        self._store_dir = None if store_dir is None else store_dir.rstrip("/")
        self._engine = engine
        self._fsops = fsops
        self._keeper = keeper
        self._workdir = workdir
        self._target = target or PageTarget()
        self._store_configured = store_configured
        self._hold_work = hold_work

    def pending(self) -> list[PendingUnit]:
        """The Units to normalize: a content with no derivative yet whose library file holds a
        page to convert, found by names and first bytes, no page decoded."""
        content = storage.content
        found: list[PendingUnit] = []
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    content.c.id,
                    content.c.unit_id,
                    content.c.retention,
                    content.c.original_sha256,
                )
                .where(
                    content.c.unit_id.is_not(None),
                    content.c.derived_hash.is_(None),
                    content.c.retention.in_(("passthrough", "keep")),
                    content.c.media_type == CBZ_MEDIA_TYPE,
                )
                .order_by(content.c.id)
            ).all()
            for row in rows:
                known = page_issue_for(connection, row.unit_id)
                if (
                    known is not None
                    and known.original_sha256 == row.original_sha256
                    and known.code != PAGES_WORKER_FAILED
                ):
                    continue  # refused for this very content already: nothing new to look at
                copies = library_copies(connection, row.unit_id)
                local = next((c.local for c in copies if not c.damaged), None)
                if local is None:
                    continue
                if self._wants(local):
                    found.append(PendingUnit(row.unit_id, row.id, local, row.original_sha256))
                elif row.retention == "keep" and self._interrupted(row, local):
                    found.append(
                        PendingUnit(row.unit_id, row.id, local, row.original_sha256, resume=True)
                    )
        return found

    def _interrupted(self, row: Any, local: str) -> bool:
        """A rewrite cut off after its rename (kill -9): the kept original has pages to convert,
        the library file is no longer it and has none left. Only the books are missing."""
        if self._store_dir is None:
            return False
        kept = f"{self._store_dir}/{keep_rel(row.unit_id, row.id)}"
        if not self._wants(kept):
            return False
        try:
            return bool(self._fsops.sha256_of(local) != row.original_sha256)
        except (OSError, FsopsError):
            return False

    def _wants(self, local: str) -> bool:
        try:
            with (
                self._fsops.open_read(local) as stream,
                safe_open_archive(stream, name=Path(local).name) as archive,
            ):
                return needs_normalization(archive)
        except (OSError, UnsafeArchiveError, ValueError, KeyError):
            return False  # missing or unreadable: the integrity checks say so, not this command

    def run(self, *, dry_run: bool = False) -> PageNormalizeSummary:
        summary = PageNormalizeSummary(dry_run)
        if not dry_run and not self._store_configured:
            raise PageStoreNotConfiguredError(STORE_NOT_CONFIGURED)
        summary.pending = self.pending()
        if dry_run:
            return summary
        for unit in summary.pending:
            summary.reports.append(self._one(unit))
        return summary

    def _one(self, unit: PendingUnit) -> UnitOutcome:
        try:
            with (
                self._fsops.open_read(unit.local) as stream,
                tempfile.NamedTemporaryFile(
                    prefix="pages-source-", suffix=".cbz", dir=self._workdir
                ) as copy,
            ):
                while chunk := stream.read(1 << 20):
                    copy.write(chunk)
                copy.flush()
                return self._declare(unit, Path(copy.name))
        except SafeguardFailedError as error:
            log.info("pages.normalize_failed", unit_id=unit.unit_id, code=error.reason)
            busy = error.reason == "work_busy"
            return UnitOutcome(
                unit.unit_id, "failed", code=PAGES_WORK_BUSY if busy else PAGES_NOT_KEPT
            )
        except ImageRejected as error:
            if error.code != WORKER_FAILED:
                raise
            note_page_issue(
                self._engine, unit.unit_id, unit.original_sha256, PAGES_WORKER_FAILED,
                died=True,
            )  # fmt: skip
            return UnitOutcome(unit.unit_id, "failed", code=PAGES_WORKER_FAILED)
        except (OSError, UnsafeArchiveError, ValueError):
            return UnitOutcome(unit.unit_id, "failed", code=PAGES_UNREADABLE)

    def _declare(self, unit: PendingUnit, staged: Path) -> UnitOutcome:
        if unit.resume:  # the file in the library is the derivative already: declare it as it is
            self._keeper.declare_derivative(unit.unit_id, staged, PAGE_DERIVED_BY, lossy=True)
            return UnitOutcome(unit.unit_id, "normalized")
        with _derivative_of(staged, self._workdir, self._target) as (derived, report):
            if report.status == "normalized":
                self._keeper.declare_derivative(unit.unit_id, derived, PAGE_DERIVED_BY, lossy=True)
                clear_page_issue(self._engine, unit.unit_id)
                return UnitOutcome(unit.unit_id, "normalized")
        if report.status == "unchanged":
            return UnitOutcome(unit.unit_id, "unchanged")
        for issue in report.issues:
            log.info("pages.not_normalized", unit_id=unit.unit_id, code=issue.code)
        if report.issues:
            note_page_issue(self._engine, unit.unit_id, sha256_file(staged), report.issues[0].code)
        return UnitOutcome(unit.unit_id, "kept_as_received", report.issues)
