"""From what the decoder read to what Shelfhold keeps (EF-INT-008).

Pure, no database. A series of the backup becomes a **pending source item** (its source is not
resolved here: that goes through the Fetcher and its guard, later); the chapters it lists are
**known units**; what the file proves read is a **read marker**; a tracker entry is a **tracker
hint**. Nothing is guessed:

* a chapter is read only when the file's flag says exactly so. A tracker saying the reader reached
  chapter 40 makes no chapter read, and a later chapter read makes no earlier one read;
* a page is a marker too, when the chapter is started and not finished;
* a hint keeps the tracker (a closed list), the entry's identifier and how far the reader was
  there. The address, the title and anything else of the tracker entry are dropped, and with them
  any secret an address could carry; the preferences of the backup (tracker tokens) are never read;
* the name of a source is never read, so it cannot be kept: a source is `mihon:<its numeric id>`;
* a title is cleaned as any upstream text (`sanitize_upstream_text`), an address or a chapter
  reference that holds a control character is left out (fail closed: it would not resolve anyway).
"""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Final

from shelfhold.infrastructure.content_trust.sanitize import sanitize_upstream_text
from shelfhold.platform.import_tachibk.decode import RawBackup, RawChapter, RawManga

LOCAL_SOURCE_ID: Final = 0  # the folder of the device: nothing a source could resolve
MAX_TITLE: Final = 300
MAX_REF: Final = 2048
MAX_UNITS_PER_SERIES: Final = 5_000
MAX_PAGE: Final = 100_000
MAX_NUMBER: Final = 100_000.0
UNIT_STATE: Final = "known"  # what a chapter the backup lists is until its source is resolved

# The ids Mihon gives its trackers; the names are the ones Shelfhold uses for the same services.
TRACKERS: Final[dict[int, str]] = {
    1: "myanimelist",
    2: "anilist",
    3: "kitsu",
    4: "shikimori",
    5: "bangumi",
    7: "mangaupdates",
}  # fmt: skip  (6 is a self-hosted server, 8 and 9 too: no hint is kept for them)


@dataclass(slots=True)
class KnownUnit:
    ref: str
    number: str | None
    state: str = UNIT_STATE


@dataclass(slots=True)
class ReadMarker:
    chapter_ref: str
    number: str | None
    read: bool
    last_page: int | None


@dataclass(slots=True)
class TrackerHint:
    tracker: str
    remote_id: str
    last_chapter: float | None


@dataclass(slots=True)
class SeriesRecord:
    source_key: str
    series_ref: str
    title: str
    units: list[KnownUnit] = field(default_factory=list)
    markers: list[ReadMarker] = field(default_factory=list)
    hints: list[TrackerHint] = field(default_factory=list)


@dataclass(slots=True)
class Staged:
    """What a preview holds and an application writes."""

    schema_version: str
    series: list[SeriesRecord] = field(default_factory=list)
    sources: int = 0
    skipped_not_favorite: int = 0
    skipped_local: int = 0
    skipped_invalid: int = 0
    skipped_trackers: int = 0
    skipped_long: int = 0
    unknown_fields: list[int] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Staged:
        raw: dict[str, Any] = json.loads(text)
        series = [
            SeriesRecord(
                s["source_key"], s["series_ref"], s["title"],
                [KnownUnit(**u) for u in s["units"]],
                [ReadMarker(**m) for m in s["markers"]],
                [TrackerHint(**h) for h in s["hints"]],
            )
            for s in raw.pop("series")
        ]  # fmt: skip
        return cls(series=series, **raw)

    @property
    def chapters(self) -> int:
        return sum(len(s.units) for s in self.series)

    @property
    def read(self) -> int:
        return sum(1 for s in self.series for m in s.markers if m.read)

    @property
    def started(self) -> int:
        return sum(1 for s in self.series for m in s.markers if not m.read)

    @property
    def trackers(self) -> int:
        return sum(len(s.hints) for s in self.series)


def _clean(ref: str) -> str | None:
    """An address or a reference as the backup wrote it, or None when it cannot be kept."""
    if not ref or len(ref) > MAX_REF or ref != ref.strip():
        return None
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") for c in ref):
        return None
    return ref


def _number(value: float | None) -> str | None:
    if value is None or not math.isfinite(value) or not 0 <= value <= MAX_NUMBER:
        return None
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _page(value: int) -> int | None:
    return value if 0 < value <= MAX_PAGE else None


def _series(manga: RawManga, staged: Staged) -> SeriesRecord | None:
    ref = _clean(manga.url)
    title = sanitize_upstream_text(manga.title, max_length=MAX_TITLE).strip()
    if ref is None or not title:
        staged.skipped_invalid += 1
        return None
    record = SeriesRecord(f"mihon:{manga.source}", ref, title)
    seen: set[str] = set()
    for chapter in manga.chapters:
        _chapter(chapter, record, seen)
    for entry in manga.tracking:
        name = TRACKERS.get(entry.tracker)
        if name is None or entry.remote_id <= 0:
            staged.skipped_trackers += 1
            continue
        last = entry.last_chapter
        plausible = last is not None and math.isfinite(last) and 0 <= last <= MAX_NUMBER
        record.hints.append(TrackerHint(name, str(entry.remote_id), last if plausible else None))
    return record


def _chapter(chapter: RawChapter, record: SeriesRecord, seen: set[str]) -> None:
    ref = _clean(chapter.url)
    if ref is None or ref in seen:
        return
    seen.add(ref)
    number = _number(chapter.number)
    if len(record.units) < MAX_UNITS_PER_SERIES:
        record.units.append(KnownUnit(ref, number))
    page = _page(chapter.last_page)
    if (chapter.read or page is not None) and len(record.markers) < MAX_UNITS_PER_SERIES:
        record.markers.append(ReadMarker(ref, number, chapter.read, None if chapter.read else page))


def map_backup(raw: RawBackup) -> Staged:
    staged = Staged(
        schema_version=raw.schema_version,
        sources=len(raw.source_ids),
        skipped_long=raw.skipped_long,
        unknown_fields=list(raw.unknown_fields),
    )
    seen: set[tuple[str, str]] = set()
    for manga in raw.manga:
        if not manga.favorite:
            staged.skipped_not_favorite += 1
        elif manga.source == LOCAL_SOURCE_ID:
            staged.skipped_local += 1
        else:
            record = _series(manga, staged)
            if record is None:
                continue
            key = (record.source_key, record.series_ref)
            if key in seen:
                staged.skipped_invalid += 1
                continue
            seen.add(key)
            staged.series.append(record)
    return staged
