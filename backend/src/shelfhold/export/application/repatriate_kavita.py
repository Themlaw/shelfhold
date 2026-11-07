"""The control before an unmount, as Kavita's API sees it (EF-EXP-051).

Copying the files home proves the bytes; it does not prove that Kavita, once the old mount is
gone, still has the same series, the same chapters and the reading progress of the family. So,
before the first file is copied, the apply writes down what Kavita says about the adopted folder
(`KavitaSnapshot`: its series, the files it indexed, the pages read of each). The control asks
Kavita to scan the folder, reads the same things again, and compares: a series or a file that
is gone, a series that appeared (a second series is the failure to fear), a file whose progress
went back. Nothing is verified when Kavita cannot be read or when there is no "before"; and then
nothing is declared safe.

The progress is the one Kavita's API gives for the account of the key (`ChapterDto.pagesRead`).
The snapshot holds Kavita's own paths and counts: no key, no URL, no name of a person.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.export.domain.path_map import clean_absolute
from shelfhold.infrastructure.schema import platform

__all__ = [
    "SETTLE_ATTEMPTS",
    "SETTLE_INTERVAL_S",
    "BaselineStore",
    "KavitaPort",
    "KavitaSnapshot",
    "KavitaVerdict",
    "compare",
    "covers",
    "read_snapshot",
    "verify",
]

SETTLE_ATTEMPTS: Final = 6  # Kavita scans on its own time: the answer is read again if it differs
SETTLE_INTERVAL_S: Final = 5.0
MAX_SNAPSHOT_FILES: Final = 200_000  # a bound on what a stored or received snapshot may hold
_KEY: Final = "repatriate.kavita_baseline."


class KavitaPort(Protocol):
    """What the control needs of the Kavita client (read routes and the targeted scan)."""

    async def scan_folder(self, remote_path: str) -> None: ...
    async def series_of_library(self, library_id: int) -> list[Any]: ...
    async def series_chapters(self, series_id: int) -> list[Any]: ...


@dataclass(frozen=True)
class KavitaSnapshot:
    """Kavita's answer for a folder: series (id -> name) and files (path -> pages read)."""

    series: dict[str, str]
    files: dict[str, int]

    def dumps(self) -> str:
        return json.dumps({"series": self.series, "files": self.files}, sort_keys=True)

    @classmethod
    def loads(cls, raw: str) -> KavitaSnapshot | None:
        """None for anything that is not a snapshot: it is read back from the database, but a
        damaged value must refuse the control, not crash it."""
        try:
            data = json.loads(raw)
            series, files = data["series"], data["files"]
            if not (isinstance(series, dict) and isinstance(files, dict)):
                return None
            if len(files) > MAX_SNAPSHOT_FILES:
                return None
            return cls(
                {str(k): str(v) for k, v in series.items()},
                {str(k): int(v) for k, v in files.items()},
            )
        except (ValueError, KeyError, TypeError):
            return None


@dataclass(frozen=True)
class KavitaVerdict:
    """The comparison. `reason` says why nothing could be verified; otherwise the lists say what
    differs (empty: the same series, the same files, no progress lost)."""

    # no_baseline | baseline_incomplete | unreachable | incomplete | no_library
    reason: str | None = None
    series_before: int = 0
    series_after: int = 0
    files_before: int = 0
    files_after: int = 0
    missing_series: tuple[str, ...] = ()
    unexpected_series: tuple[str, ...] = ()
    missing_files: tuple[str, ...] = ()
    unexpected_files: tuple[str, ...] = ()
    progress_lost: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.reason is None

    @property
    def ok(self) -> bool:
        return self.verified and not (
            self.missing_series or self.unexpected_series or self.missing_files
            or self.unexpected_files or self.progress_lost
        )  # fmt: skip

    @property
    def problems(self) -> tuple[tuple[str, str], ...]:
        """(kind, what) for every difference, in a stable order."""
        found: list[tuple[str, str]] = []
        for kind, items in (
            ("missing_series", self.missing_series), ("unexpected_series", self.unexpected_series),
            ("missing_file", self.missing_files), ("unexpected_file", self.unexpected_files),
            ("progress_lost", self.progress_lost),
        ):  # fmt: skip
            found.extend((kind, item) for item in items)
        return tuple(found)


def compare(before: KavitaSnapshot, after: KavitaSnapshot) -> KavitaVerdict:
    gone = before.series.keys() - after.series.keys()  # by id: a series rebuilt under a new id
    born = after.series.keys() - before.series.keys()  # lost its progress, whatever its name
    return KavitaVerdict(
        series_before=len(before.series), series_after=len(after.series),
        files_before=len(before.files), files_after=len(after.files),
        missing_series=tuple(sorted(before.series[i] for i in gone)),
        unexpected_series=tuple(sorted(after.series[i] for i in born)),
        missing_files=tuple(sorted(set(before.files) - set(after.files))),
        unexpected_files=tuple(sorted(set(after.files) - set(before.files))),
        progress_lost=tuple(sorted(
            path for path, read in before.files.items()
            if path in after.files and after.files[path] < read
        )),
    )  # fmt: skip


def covers(
    snapshot: KavitaSnapshot, remote_root: str, rel_paths: Sequence[str], series: int
) -> bool:
    """Does the "before" hold what the adopted files are? At least `series` series and every file
    of the plan under `remote_root`. An empty answer (the account sees nothing, or Kavita is
    still scanning) covers nothing, and a comparison with it would prove nothing."""
    if not snapshot.series or len(snapshot.series) < series or not rel_paths:
        return False
    root = clean_absolute(remote_root)
    return all(f"{root}/{rel}" in snapshot.files for rel in rel_paths)


async def read_snapshot(client: KavitaPort, library_ref: str, remote_root: str) -> KavitaSnapshot:
    """The series of the library living under `remote_root`, their files and pages read."""
    root = clean_absolute(remote_root)
    series: dict[str, str] = {}
    files: dict[str, int] = {}
    for item in await client.series_of_library(int(library_ref)):
        where = (item.lowest_folder_path or item.folder_path).rstrip("/")
        if where != root and not where.startswith(root + "/"):
            continue
        series[str(item.id)] = str(item.name)
        for chapter in await client.series_chapters(item.id):
            files[chapter.path] = chapter.pages_read
    return KavitaSnapshot(series, files)


class BaselineStore:
    """The "before" of a destination, in `app_setting`; written once, never overwritten by a
    later apply (that one could be run after the mount went, and would record the damage)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def key(destination_id: str) -> str:
        return f"{_KEY}{destination_id}"

    def load(self, destination_id: str) -> KavitaSnapshot | None:
        with self._engine.connect() as connection:
            raw = connection.execute(
                sa.select(platform.app_setting.c.value).where(
                    platform.app_setting.c.key == self.key(destination_id)
                )
            ).scalar()
        return None if raw is None else KavitaSnapshot.loads(raw)

    def save_if_absent(self, destination_id: str, snapshot: KavitaSnapshot) -> bool:
        with self._engine.begin() as connection:
            inserted = connection.execute(
                sqlite_insert(platform.app_setting)
                .values(key=self.key(destination_id), value=snapshot.dumps())
                .on_conflict_do_nothing(index_elements=["key"])
            ).rowcount
        return bool(inserted)


async def verify(
    client: KavitaPort,
    baseline: KavitaSnapshot | None,
    library_ref: str | None,
    remote_root: str,
    *,
    unreachable: tuple[type[Exception], ...],
    sleep: Callable[[float], Any] = asyncio.sleep,
    scan_allowed: Callable[[str], bool] | None = None,
) -> KavitaVerdict:
    """Scan, then read until Kavita agrees with the baseline or the attempts are spent.

    `scan_allowed(remote_root)` is the sentinel's word on the folder (EF-EXP-029): a scan of a
    folder whose mount is not the expected one would make Kavita drop its series, so it is not
    sent and the check says so."""
    if baseline is None:
        return KavitaVerdict(reason="no_baseline")
    if library_ref is None:
        return KavitaVerdict(reason="no_library")
    if scan_allowed is not None and not scan_allowed(remote_root):
        return KavitaVerdict(reason="scan_refused")
    try:
        await client.scan_folder(remote_root)
        verdict = KavitaVerdict(reason="unreachable")
        for attempt in range(SETTLE_ATTEMPTS):
            verdict = compare(baseline, await read_snapshot(client, library_ref, remote_root))
            if verdict.ok:
                return verdict
            if attempt + 1 < SETTLE_ATTEMPTS:
                await sleep(SETTLE_INTERVAL_S)
    except unreachable as error:  # a listing cut at its cap is not "all of Kavita"
        return KavitaVerdict(
            reason="incomplete" if getattr(error, "incomplete", False) else "unreachable"
        )
    return verdict
