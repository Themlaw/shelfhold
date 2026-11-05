"""Switching over from an existing automation (ADR-0034, EF-EXP-044, EF-EXP-051).

Many admins already feed Kavita's folders straight from Suwayomi (its download folder mounted
there) and run scripts that rename, de-duplicate and set covers. Shelfhold never lives next to
that on the same folders: a series that looks fed by Suwayomi is *detected* and the follow of its
shelf is refused until the admin has confirmed the documented procedure, step by step. Nothing
here writes to the library: detection reads, the confirmation is one line in `app_setting`.

The signals are heuristics, shown to the admin as evidence and never as proof: the
`<team>_<chapter>` file name of Suwayomi, and the `Translator`, `Web` or `Notes` fields that a
ComicInfo written by Suwayomi carries and Shelfhold's own never does.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.domain.path_map import PathMode, clean_absolute, within
from shelfhold.infrastructure.archives.safe_open import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import export, platform
from shelfhold.infrastructure.xml.safe_parse import UnsafeXmlError, safe_parse_xml

__all__ = [
    "PROCEDURE_DOC",
    "STEPS",
    "DownloadDirVerdict",
    "FeedFinding",
    "SwitchoverDetector",
    "SwitchoverGate",
    "SwitchoverIncompleteError",
    "SwitchoverReport",
    "SwitchoverUnconfirmed",
]

PROCEDURE_DOC: Final = "docs/admin/migration-depuis-scripts.md"
# The attestations of the procedure, in order (EF-EXP-044): 1 adopt, 2 pair, 3 bring back the
# files kept outside `/library` (EF-EXP-051), then remove the direct mounts, 4 stop the scripts.
STEPS: Final = (
    "adopted",
    "pairing_reviewed",
    "repatriated",
    "direct_mounts_removed",
    "scripts_disabled",
)
SAMPLE: Final = 5  # ComicInfo files read per series
SUWAYOMI_NAME: Final = re.compile(r"^[^_/]{1,80}_[^_/].*\.cbz$", re.IGNORECASE)
SUWAYOMI_FIELDS: Final = ("Translator", "Web")
SUWAYOMI_NOTES: Final = re.compile(r"suwayomi|tachidesk", re.IGNORECASE)


@dataclass(frozen=True)
class FeedFinding:
    export_series_id: str
    series_dir: str
    signals: tuple[str, ...]  # `name_motif`, `comicinfo`


@dataclass(frozen=True)
class SwitchoverReport:
    series: tuple[FeedFinding, ...]

    @property
    def detected(self) -> bool:
        return bool(self.series)


@dataclass(frozen=True)
class DownloadDirVerdict:
    """Where Suwayomi writes, against the library (EF-EXP-051): in a folder under `/library`
    only its setting changes; outside, the adopted files must first be brought back."""

    kind: Literal["under_library", "outside_library"]
    copy_needed: bool
    action: str


class SwitchoverUnconfirmed(Exception):
    """A shelf fed directly by Suwayomi: the follow waits for the confirmed procedure."""

    code = "pairing.switchover_unconfirmed"
    procedure = PROCEDURE_DOC

    def __init__(self, report: SwitchoverReport) -> None:
        names = ", ".join(sorted(f.series_dir for f in report.series))
        super().__init__(f"fed directly by Suwayomi ({names}): follow {PROCEDURE_DOC} first")
        self.report = report


class SwitchoverIncompleteError(Exception):
    def __init__(self, missing: Sequence[str]) -> None:
        super().__init__(f"steps not confirmed: {', '.join(missing)}")
        self.missing = tuple(missing)


def _tags(fsops: Fsops, local: str) -> dict[str, str]:
    """The Suwayomi-specific fields of the ComicInfo of an archive (untrusted input)."""
    try:
        with fsops.open_read(local) as stream, safe_open_archive(stream, name=local) as archive:
            name = next(
                (e.name for e in archive.entries if e.name.lower() == "comicinfo.xml"), None
            )
            if name is None:
                return {}
            root = safe_parse_xml(archive.read(name, max_bytes=1 << 20))
    except (UnsafeArchiveError, UnsafeXmlError, KeyError, OSError):
        return {}
    found: dict[str, str] = {}
    for tag in (*SUWAYOMI_FIELDS, "Notes"):
        element = root.find(tag)
        if element is not None and (element.text or "").strip():
            found[tag] = (element.text or "").strip()
    return found


class SwitchoverDetector:
    def __init__(self, engine: Engine, fsops: Fsops) -> None:
        self._engine = engine
        self._fsops = fsops

    def scan(self, destination_id: str) -> SwitchoverReport:
        """The adopted series of a destination whose folder looks fed by Suwayomi."""
        series = export.export_series
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
            rows = connection.execute(
                sa.select(series.c.id, series.c.series_dir)
                .where(
                    series.c.shelf_destination_id == destination_id,
                    series.c.frozen_at.isnot(None),
                    series.c.series_dir.isnot(None),
                )
                .order_by(series.c.id)
            ).all()
        found = []
        for row in rows:
            signals = self._signals(ctx, row.series_dir)
            if signals:
                found.append(FeedFinding(row.id, row.series_dir, signals))
        return SwitchoverReport(tuple(found))

    def _signals(self, ctx: DestinationContext, series_dir: str) -> tuple[str, ...]:
        if ctx.root_local is None:
            return ()
        local = f"{ctx.root_local}/{series_dir}"
        try:
            entries = self._fsops.list_dir(local)
        except OSError:
            return ()
        archives = sorted(
            n for n, kind in entries.items() if kind == "file" and n.lower().endswith(".cbz")
        )
        signals: list[str] = []
        motif = [n for n in archives if SUWAYOMI_NAME.match(n)]
        if archives and 2 * len(motif) >= len(archives):
            signals.append("name_motif")
        for name in archives[:SAMPLE]:
            tags = _tags(self._fsops, f"{local}/{name}")
            if any(t in tags for t in SUWAYOMI_FIELDS) or SUWAYOMI_NOTES.search(
                tags.get("Notes", "")
            ):
                signals.append("comicinfo")
                break
        return tuple(signals)

    def classify_download_dir(self, destination_id: str, download_dir: str) -> DownloadDirVerdict:
        """Where Suwayomi writes (a path as this process sees it), against the library roots
        of the target's path map. Nothing is read from, copied to or moved in either place."""
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
        path = clean_absolute(download_dir)
        writable = [e.local for e in ctx.path_map.entries if e.mode is PathMode.RW]
        if any(within(path, root) is not None for root in writable):
            return DownloadDirVerdict(
                "under_library",
                False,
                "Change the download folder in Suwayomi to this folder; the files stay where "
                "they are and nothing is copied.",
            )
        return DownloadDirVerdict(
            "outside_library",
            True,
            "The adopted files live outside the library: run `shelfhold adopt --repatriate "
            "--dry-run`, then bring them back before removing the mount.",
        )


class SwitchoverGate:
    """The attestation of the procedure, one per destination (no migration: `app_setting`)."""

    def __init__(self, engine: Engine, detector: SwitchoverDetector, *, clock: Clock) -> None:
        self._engine = engine
        self._detector = detector
        self._clock = clock

    @staticmethod
    def _key(destination_id: str) -> str:
        return f"switchover.confirmed.{destination_id}"

    def confirm(self, destination_id: str, steps: Sequence[str]) -> None:
        """Every step of the procedure, or none: a partial list is refused and kept nowhere."""
        missing = [s for s in STEPS if s not in steps]
        if missing:
            raise SwitchoverIncompleteError(missing)
        value: dict[str, Any] = {"steps": list(STEPS), "at": self._clock.now().isoformat()}
        with self._engine.begin() as connection:
            connection.execute(
                sqlite_insert(platform.app_setting)
                .values(key=self._key(destination_id), value=json.dumps(value))
                .on_conflict_do_update(index_elements=["key"], set_={"value": json.dumps(value)})
            )

    def is_confirmed(self, destination_id: str) -> bool:
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(platform.app_setting.c.value).where(
                        platform.app_setting.c.key == self._key(destination_id)
                    )
                ).scalar()
                is not None
            )

    def require_clear(self, destination_id: str) -> None:
        """Refuse the follow of a shelf fed directly by Suwayomi until the steps are confirmed."""
        if self.is_confirmed(destination_id):
            return
        report = self._detector.scan(destination_id)
        if report.detected:
            raise SwitchoverUnconfirmed(report)
