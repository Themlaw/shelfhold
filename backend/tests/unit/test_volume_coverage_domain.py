"""EF-BIB-044, EF-EXP-019 (M4 L10): what the volume assembled from chapters covers.

The bounds of each chapter in the assembled file are a pure function of the page count of the
chapters, in reading order: contiguous, none shared, none lost. A position on a chapter is carried
to the volume by those bounds, and a page of the volume is found back in its chapter. The hash of
the volume says what it was made of, never a content received from a source.
"""

import pytest

from shelfhold.library.domain.coverage import (
    ChapterSpan,
    VolumeCoverageError,
    derived_content_hash,
    spans_of,
    unit_at,
    volume_page_of,
)

pytestmark = pytest.mark.req("EF-BIB-044", "EF-EXP-019")

A, B, C = "01HZZZZZZZZZZZZZZZZZZZZZZA", "01HZZZZZZZZZZZZZZZZZZZZZZB", "01HZZZZZZZZZZZZZZZZZZZZZZC"


def test_the_bounds_are_contiguous_and_follow_the_reading_order() -> None:
    spans = spans_of([(A, 4), (B, 3), (C, 5)])

    assert spans == (
        ChapterSpan(A, 1, 1, 4),
        ChapterSpan(B, 2, 5, 7),
        ChapterSpan(C, 3, 8, 12),
    )


def test_a_chapter_of_one_page_has_equal_bounds() -> None:
    assert spans_of([(A, 1)]) == (ChapterSpan(A, 1, 1, 1),)


@pytest.mark.parametrize("pages", [0, -1])
def test_a_chapter_without_pages_is_refused(pages: int) -> None:
    with pytest.raises(VolumeCoverageError):
        spans_of([(A, 3), (B, pages)])


def test_the_same_chapter_twice_is_refused() -> None:
    with pytest.raises(VolumeCoverageError):
        spans_of([(A, 3), (A, 2)])


def test_nothing_to_assemble_is_refused() -> None:
    with pytest.raises(VolumeCoverageError):
        spans_of([])


def test_a_page_of_the_volume_is_found_in_its_chapter() -> None:
    spans = spans_of([(A, 4), (B, 3)])

    assert unit_at(spans, 1) == (A, 1)
    assert unit_at(spans, 4) == (A, 4)
    assert unit_at(spans, 5) == (B, 1)  # the first page of the next chapter, not the last of this
    assert unit_at(spans, 7) == (B, 3)
    assert unit_at(spans, 0) is None and unit_at(spans, 8) is None


def test_a_position_on_a_chapter_is_carried_to_the_volume() -> None:
    spans = spans_of([(A, 4), (B, 3)])

    assert volume_page_of(spans, B, 2) == 6
    assert volume_page_of(spans, A, 1) == 1


def test_a_page_outside_its_chapter_is_not_carried() -> None:
    spans = spans_of([(A, 4), (B, 3)])

    assert volume_page_of(spans, B, 4) is None  # the chapter has three pages
    assert volume_page_of(spans, B, 0) is None
    assert volume_page_of(spans, C, 1) is None  # not a member


def test_the_derived_hash_depends_on_the_members_and_their_order() -> None:
    one = derived_content_hash(["sha256:" + "a" * 64, "sha256:" + "b" * 64])
    swapped = derived_content_hash(["sha256:" + "b" * 64, "sha256:" + "a" * 64])
    fewer = derived_content_hash(["sha256:" + "a" * 64])

    assert one.startswith("sha256:") and len(one) == 71
    assert len({one, swapped, fewer}) == 3
    assert one == derived_content_hash(["sha256:" + "a" * 64, "sha256:" + "b" * 64])
    assert one != "sha256:" + "a" * 64  # never the hash of a content that was received
