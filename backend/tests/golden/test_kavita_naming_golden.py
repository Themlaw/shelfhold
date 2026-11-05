"""Golden `plugin/parse` outcomes of the file names Shelfhold writes (ENF-QUAL-004, EF-EXP-045).

The parser is the documented replica in tests/fakes/kavita/plugin_parse.py; the nightly (L40)
replays the same names against a real Kavita. Set SHELFHOLD_UPDATE_GOLDEN=1 to rewrite the files.
"""

import json
import os
from pathlib import Path

import pytest

from shelfhold.export.domain.naming import FrozenAttrs, export_path, volume_deduced
from tests.fakes.kavita.plugin_parse import simulate_parse

GOLDEN = Path(__file__).parent / "kavita_naming"


def _case(spec: dict[str, object]) -> dict[str, object]:
    frozen = FrozenAttrs(**{k: v for k, v in spec.items() if k != "upstream_title"})  # type: ignore[arg-type]
    path = export_path(frozen)
    parsed = simulate_parse(path.filename)
    return {
        "input": spec,
        "rel_path": path.rel_path,
        "series": parsed.series_name,
        "chapter": parsed.min_chapter,
        "volume": parsed.min_volume,
        "volume_deduced": volume_deduced(parsed),
    }


def _check(name: str, specs: list[dict[str, object]]) -> list[dict[str, object]]:
    actual = [_case(s) for s in specs]
    target = GOLDEN / name
    if os.environ.get("SHELFHOLD_UPDATE_GOLDEN") == "1":
        target.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n", "utf-8")
    assert json.loads(target.read_text("utf-8")) == actual
    return actual


CHAPTERS: list[dict[str, object]] = [
    {"series": "Dr Stone", "track": "main", "number_norm": "311", "upstream_title": "Chapter 311 - {S1 END}"},
    {"series": "Dr Stone", "track": "main", "number_norm": "12", "upstream_title": "Chapter 12 [S1 END]"},
    {"series": "Dr Stone", "track": "main", "number_norm": "40", "upstream_title": "Ch.40 - S5 Start"},
    {"series": "Dr Stone", "track": "main", "number_norm": "10.5"},
    {"series": "One Piece", "track": "main", "number_norm": "1"},
    {"series": "Who? Am I", "track": "main", "number_norm": "7"},
    {"series": "Dr Stone", "track": "main", "number_norm": "0"},
]


@pytest.mark.req("EF-EXP-045", "EF-EXP-014", "ENF-QUAL-004")
def test_chapter_name_has_no_volume_golden_plugin_parse() -> None:
    """Name only: the route reads neither ComicInfo nor the folder."""
    actual = _check("chapters.json", CHAPTERS)
    for case in actual:
        if not str(case["rel_path"]).endswith("Special 0.cbz"):
            assert case["volume_deduced"] is False, case


SEGMENTS: list[dict[str, object]] = [
    {"series": "Tower of God", "track": "main", "number_norm": "1", "segment": ""},
    {"series": "Tower of God", "track": "main", "number_norm": "1", "segment": "S2"},
    {"series": "Tower of God", "track": "main", "number_norm": "40", "segment": "S2"},
    {"series": "Tower of God", "track": "main", "number_norm": "1", "segment": "S10"},
]


@pytest.mark.req("EF-BIB-050", "EF-EXP-045", "ENF-QUAL-004")
def test_segment_naming_golden_plugin_parse() -> None:
    """Q50, option 1: a segment is part of the exported SERIES name, never a volume marker."""
    actual = _check("segments.json", SEGMENTS)
    assert all(case["volume_deduced"] is False for case in actual)
    paths = [str(case["rel_path"]) for case in actual]
    assert len(set(paths)) == len(paths)
    series = {str(case["series"]) for case in actual}
    assert len(series) == 3  # three distinct Kavita series: no two files of the same number
