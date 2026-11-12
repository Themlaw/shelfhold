"""The integrity check of the Units' archives (EF-BIB-012): ZIP structure and CRC, page count, hash.

A CBZ is read through `safe_open_archive` (ENF-SEC-002), the one entry point for an archive nobody
vouches for: a trap in it (a bomb, a `..` name, a link, a lying header, a truncated file) is a
failed check, never an exception and never an extraction. The checks, in this order, stop at the
first that fails: the archive and every entry read to its end (its CRC), the number of pages next
to the one recorded at reception, the hash of the file next to the manifest's.

The check is for the maintenance window (ENF-OPS-014): `guard` is called before any file is
opened and refuses outside it; the reads are paced by a `Throttle`; a Unit checked recently is not
due again, so the disks are not woken every cycle. A file that is not there is not "corrupt" (the
store verification says so, not this check) and changes nothing. This module marks and says; it
never deletes, replaces or moves a file. What follows a corrupt mark is the reacquisition
(`acquisition.application.reacquire`).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.archives import (
    DEFAULT_LIMITS,
    ArchiveLimits,
    UnsafeArchiveError,
    safe_open_archive,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.content_trust.cbz_entries import (
    UntrustedEntryError,
    check_cbz_entries,
)
from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.library.replica import library_copies, store_copies

__all__ = [
    "DEFAULT_RATE",
    "FileVerdict",
    "IntegrityChecker",
    "Throttle",
    "UnitVerdict",
    "check_cbz",
]

log = structlog.get_logger(__name__)

DEFAULT_RATE: Final = 25 * 1024 * 1024  # bytes per second: a check never runs flat out
RECHECK_AFTER: Final = timedelta(days=30)
CORRUPT_EVENT: Final = "ops.integrity_corrupt"
_CHUNK: Final = 1024 * 1024
REASONS: Final = frozenset({"unsafe_archive", "crc", "page_count", "hash", "unreadable"})


class Throttle:
    """Keeps the reads under `bytes_per_s` on average; None does not slow anything down."""

    def __init__(
        self,
        bytes_per_s: int | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate = bytes_per_s
        self._clock = clock
        self._sleep = sleep
        self._start = clock()
        self._spent = 0

    def spend(self, count: int) -> None:
        if self._rate is None or self._rate <= 0:
            return
        self._spent += count
        wait = self._start + self._spent / self._rate - self._clock()
        if wait > 0:
            self._sleep(wait)


@dataclass(frozen=True, slots=True)
class FileVerdict:
    ok: bool
    reason: str | None = None  # one of REASONS when not ok


def _bare(digest: str) -> str:
    return digest.removeprefix("sha256:").lower()


def _read_to_end(archive: object, name: str, throttle: Throttle | None) -> None:
    with archive.open(name) as stream:  # type: ignore[attr-defined]
        buffer = bytearray(_CHUNK)
        while got := stream.readinto(buffer):
            if throttle is not None:
                throttle.spend(got)


def check_cbz(
    path: Path,
    *,
    expected_pages: int | None,
    expected_hash: str | None,
    throttle: Throttle | None = None,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> FileVerdict:
    """The verdict on one archive; a missing file raises `FileNotFoundError` (nothing to check)."""
    try:
        with safe_open_archive(path, limits=limits) as archive:
            listing = check_cbz_entries(archive, allow_svg=True)
            for entry in archive.entries:
                if not entry.is_dir:
                    _read_to_end(archive, entry.name, throttle)  # the CRC is checked at the end
            pages = len(listing.pages)
    except UnsafeArchiveError as error:
        return FileVerdict(False, "crc" if error.reason == "corrupt" else "unsafe_archive")
    except UntrustedEntryError:
        return FileVerdict(False, "unsafe_archive")
    except FileNotFoundError:
        raise
    except OSError:
        return FileVerdict(False, "unreadable")
    if expected_pages is not None and pages != expected_pages:
        return FileVerdict(False, "page_count")
    if expected_hash is not None:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(_CHUNK):
                    digest.update(chunk)
                    if throttle is not None:
                        throttle.spend(len(chunk))
        except OSError:
            return FileVerdict(False, "unreadable")
        if digest.hexdigest() != _bare(expected_hash):
            return FileVerdict(False, "hash")
    return FileVerdict(True)


@dataclass(frozen=True, slots=True)
class UnitVerdict:
    unit_id: str
    state: str  # `ok`, `corrupt`, or what it was when nothing could be checked
    reason: str | None = None
    checked: int = 0  # copies actually read


@dataclass(frozen=True, slots=True)
class _Target:
    path: Path
    expected_pages: int | None
    expected_hash: str | None


class IntegrityChecker:
    def __init__(
        self,
        engine: Engine,
        bus: EventBus,
        clock: Clock,
        *,
        throttle: Throttle | None = None,
        guard: Callable[[], None] | None = None,
        recheck_after: timedelta = RECHECK_AFTER,
        limits: ArchiveLimits = DEFAULT_LIMITS,
    ) -> None:
        self._engine = engine
        self._bus = bus
        self._clock = clock
        self._throttle = throttle
        self._guard = guard
        self._recheck_after = recheck_after
        self._limits = limits

    def due_units(self, limit: int) -> list[str]:
        """Never checked first, then the longest ago; one checked within the interval is not due.
        A read of the database: no disk is woken."""
        if limit <= 0:
            return []
        unit, content = catalog.unit, storage.content
        before = self._clock.now() - self._recheck_after
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(unit.c.id)
                .where(
                    sa.exists().where(content.c.unit_id == unit.c.id, content.c.format == "cbz"),
                    sa.or_(
                        unit.c.integrity_checked_at.is_(None), unit.c.integrity_checked_at < before
                    ),
                )
                .order_by(unit.c.integrity_checked_at.is_not(None), unit.c.integrity_checked_at,
                          unit.c.id)
                .limit(limit)
            )  # fmt: skip
            return [str(r[0]) for r in rows]

    def _targets(self, unit_id: str, content_id: str | None) -> list[_Target]:
        content = storage.content
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(content).where(content.c.unit_id == unit_id, content.c.format == "cbz")
            ).all()
            library = list(library_copies(connection, unit_id))
            store = list(store_copies(connection, unit_id))
        pages = {r.content_hash: r.page_count for r in rows if r.content_hash}
        by_original = {r.original_sha256: r.page_count for r in rows}
        wanted = next((r for r in rows if r.id == content_id), None) if content_id else None
        found: list[_Target] = []
        for copy in library:
            if wanted is not None and copy.content_hash != wanted.content_hash:
                continue
            found.append(
                _Target(Path(copy.local), pages.get(copy.content_hash or ""), copy.file_hash)
            )
        if wanted is None:
            for kept in store:
                found.append(
                    _Target(Path(kept.local), by_original.get(kept.file_hash or ""), kept.file_hash)
                )
        return found

    def _verdicts(self, targets: list[_Target]) -> tuple[list[FileVerdict], int]:
        if self._guard is not None and targets:
            self._guard()  # the window: before any disk is touched
        verdicts: list[FileVerdict] = []
        for target in targets:
            try:
                verdicts.append(
                    check_cbz(
                        target.path,
                        expected_pages=target.expected_pages,
                        expected_hash=target.expected_hash,
                        throttle=self._throttle,
                        limits=self._limits,
                    )
                )
            except FileNotFoundError:
                continue  # not here: the store verification's business, not a corrupt archive
        return verdicts, len(verdicts)

    def check_unit(self, unit_id: str) -> UnitVerdict:
        """Check every copy of the Unit; mark it `ok` or `corrupt` and say so once."""
        verdicts, count = self._verdicts(self._targets(unit_id, None))
        unit = catalog.unit
        announced = []
        with self._engine.begin() as connection:
            previous = connection.execute(
                sa.select(unit.c.integrity_state).where(unit.c.id == unit_id)
            ).scalar_one()
            if count == 0:
                return UnitVerdict(unit_id, str(previous), "missing", 0)  # nothing was read
            failed = next((v for v in verdicts if not v.ok), None)
            state = "ok" if failed is None else "corrupt"
            connection.execute(
                sa.update(unit)
                .where(unit.c.id == unit_id)
                .values(integrity_state=state, integrity_checked_at=self._clock.now())
            )
            if failed is not None and previous != "corrupt":
                announced.append(
                    self._bus.publish(
                        connection, CORRUPT_EVENT, {"unit_id": unit_id, "reason": failed.reason}
                    )
                )
        self._bus.deliver(announced)
        reason = failed.reason if failed is not None else None
        log.info("integrity.checked", unit_id=unit_id, state=state, reason=reason)
        return UnitVerdict(unit_id, state, reason, count)

    def verify_content(self, unit_id: str, content_id: str) -> bool:
        """The copies of one content are all intact (a fresh copy, before it replaces anything)."""
        targets = self._targets(unit_id, content_id)
        verdicts, count = self._verdicts(targets)
        return count > 0 and all(v.ok for v in verdicts)
