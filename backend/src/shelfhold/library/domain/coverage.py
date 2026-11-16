"""What a volume assembled from chapters covers (I20, EF-BIB-044, EF-EXP-019, M4 L10).

A volume made of chapters carries each of them between two pages: the bounds are a pure function
of the page count of the chapters, in reading order. They are written in `unit_coverage`
(`start_locator.page`, `end_locator.page`, both inclusive, pages counted from 1 as a reader counts
them), which is also what `reading_progress` reads to say which chapter a page of the volume is in.

The same bounds carry a position the other way: a page of a chapter is a page of the volume. The
hash of the volume says what it was made of; it is never the hash of a content a source sent, so
the volume is never taken for a copy of one of its chapters.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "ChapterSpan",
    "VolumeCoverageError",
    "derived_content_hash",
    "spans_of",
    "unit_at",
    "volume_page_of",
]

_DERIVED_TAG = b"shelfhold.volume-assembly.v1"


class VolumeCoverageError(ValueError):
    """The chapters given cannot make a volume (none, twice the same, one without pages)."""


@dataclass(frozen=True)
class ChapterSpan:
    unit_id: str
    position: int  # 1-based rank in the volume, the `position` of `unit_coverage`
    start_page: int  # first page of the chapter in the volume, from 1
    end_page: int  # last page of the chapter in the volume, inclusive

    @property
    def pages(self) -> int:
        return self.end_page - self.start_page + 1


def spans_of(chapters: Sequence[tuple[str, int]]) -> tuple[ChapterSpan, ...]:
    """`(unit_id, page count)` in reading order -> the bounds of each chapter in the volume:
    contiguous, none shared, none lost."""
    if not chapters:
        raise VolumeCoverageError("a volume needs chapters")
    seen: set[str] = set()
    spans: list[ChapterSpan] = []
    first = 1
    for rank, (unit_id, pages) in enumerate(chapters, start=1):
        if unit_id in seen:
            raise VolumeCoverageError(f"chapter {unit_id} twice")
        if pages < 1:
            raise VolumeCoverageError(f"chapter {unit_id} has no page")
        seen.add(unit_id)
        spans.append(ChapterSpan(unit_id, rank, first, first + pages - 1))
        first += pages
    return tuple(spans)


def unit_at(spans: Sequence[ChapterSpan], page: int) -> tuple[str, int] | None:
    """The chapter a page of the volume is in, and the page in that chapter (from 1)."""
    for span in spans:
        if span.start_page <= page <= span.end_page:
            return span.unit_id, page - span.start_page + 1
    return None


def volume_page_of(spans: Sequence[ChapterSpan], unit_id: str, page: int) -> int | None:
    """A page of a chapter (from 1) as a page of the volume; None when the chapter is not a member
    or the page is not one of its pages."""
    for span in spans:
        if span.unit_id == unit_id:
            return span.start_page + page - 1 if 1 <= page <= span.pages else None
    return None


def derived_content_hash(member_hashes: Iterable[str]) -> str:
    """`sha256:<hex>` over the content hashes of the members, in order: what the volume was made
    of. Tagged, so that it is never the hash of a content that was received."""
    digest = hashlib.sha256(_DERIVED_TAG)
    for member in member_hashes:
        digest.update(b"\0" + member.encode())
    return "sha256:" + digest.hexdigest()
