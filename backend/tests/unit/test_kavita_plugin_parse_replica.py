"""The replica of `plugin/parse` follows Kavita v0.9.1.4 (`MangaVolumeRegex`, `MangaChapterRegex`).

Each case is a marker the pinned parser reads (or does not read) in a name. The nightly (L40) is
what confirms the replica against a real Kavita.
"""

import pytest

from tests.fakes.kavita.plugin_parse import (
    LOOSE_LEAF,
    MANGA_CHAPTER_REGEX,
    MANGA_VOLUME_REGEX,
    simulate_parse,
)

VOLUME = [
    ("Series T3 - Ch.1.cbz", 3.0),  # `T\d+`
    ("Series 第2巻.cbz", 2.0),  # N巻
    ("Series 第3卷.cbz", 3.0),
    ("Series 第4册.cbz", 4.0),
    ("Series 63권.cbz", 63.0),
    ("Series 시즌2.cbz", 2.0),
    ("Series Том 5.cbz", 5.0),
    ("Series เล่ม 6.cbz", 6.0),
    ("Series_vol_007.cbz", 7.0),  # vol_
    ("Series v1-2.cbz", 1.0),  # range
    ("Series_v03_c05.cbz", 3.0),  # `_` as a bound
    ("Series S01 014.cbz", 1.0),
    ("Series {S1 END}.cbz", 1.0),
    ("Series Vol.3 Ch.1.cbz", 3.0),
    ("Series - Vol. 30 Ch. 187.5.cbz", 30.0),
]
NO_VOLUME = [
    "Series - Ch.12.cbz",
    "Series Season 2 - Ch.1.cbz",  # "Season N" is not a volume in 0.9.1.4
    "Series - Season 2 Ch.1.cbz",
    "Series - Ch.10 Part 2.cbz",
    "Series Saga 2 - Ch.1.cbz",
]


@pytest.mark.req("EF-EXP-045")
@pytest.mark.parametrize(("name", "volume"), VOLUME)
def test_replica_reads_every_volume_marker_of_0_9_1_4(name: str, volume: float) -> None:
    assert simulate_parse(name).min_volume == volume


@pytest.mark.req("EF-EXP-045")
@pytest.mark.parametrize("name", NO_VOLUME)
def test_replica_reads_no_volume_where_0_9_1_4_reads_none(name: str) -> None:
    assert simulate_parse(name).min_volume == LOOSE_LEAF


@pytest.mark.req("EF-EXP-045")
def test_replica_ranges_chapters_and_has_no_special() -> None:
    parsed = simulate_parse("Series - Vol.1-2 c3-5.cbz")
    assert (parsed.min_volume, parsed.max_volume) == (1.0, 2.0)
    assert (parsed.min_chapter, parsed.max_chapter) == (3.0, 5.0)
    assert simulate_parse("Series - Ch.10.5.cbz").min_chapter == 10.5
    assert simulate_parse("Series - Ch.0007.cbz").min_chapter == 7.0
    # ParseText has no notion of special: nothing in the answer says "special"
    assert not hasattr(parsed, "is_special")


@pytest.mark.req("EF-EXP-045")
def test_replica_tables_are_the_pinned_ones() -> None:
    assert len(MANGA_VOLUME_REGEX) == 18
    assert len(MANGA_CHAPTER_REGEX) == 17
