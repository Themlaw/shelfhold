"""A series name that makes Kavita deduce a volume is refused, never written (EF-EXP-045).

The name of the series is the user's: `Re Zero S2` is read as volume 2. Golden outcomes against the
replica of `plugin/parse` v0.9.1.4; the nightly (L40) replays them against a real Kavita.
"""

import json
import os
from pathlib import Path

import pytest

from shelfhold.export import errors as export_errors
from shelfhold.export.domain.naming import (
    FrozenAttrs,
    NamingGuardError,
    SeriesNameYieldsVolume,
    checked_export_path,
)
from tests.fakes.kavita.plugin_parse import simulate_parse

GOLDEN = Path(__file__).parent / "kavita_naming" / "series_guard.json"
SERIES = [
    "Re Zero S2",
    "Tower of God T1",
    "Hunter x Hunter v3",
    "Series Vol.3",
    "Naruto 第2巻",
    "Kaguya 시즌2",
    "Dr Stone {S1 END}",
    "Dr Stone",
    "Re Zero",
    "Hunter x Hunter",
    "Dr Stone Arc",
]


def outcome(series: str, segment: str = "") -> dict[str, str]:
    frozen = FrozenAttrs(series=series, track="main", number_norm="12", segment=segment)
    try:
        return {"rel_path": checked_export_path(frozen, simulate_parse).rel_path}
    except NamingGuardError as error:
        return {"refused": error.code, "component": error.component}


@pytest.mark.req("EF-EXP-045", "ENF-QUAL-004")
def test_series_name_that_yields_a_volume_is_refused_golden() -> None:
    actual = {s: outcome(s) for s in SERIES}
    actual["Tower of God | S2"] = outcome("Tower of God", "S2")
    actual["Re Zero S2 | S3"] = outcome("Re Zero S2", "S3")
    if os.environ.get("SHELFHOLD_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n", "utf-8")
    assert json.loads(GOLDEN.read_text("utf-8")) == actual
    for series in SERIES[:7]:
        assert actual[series]["refused"] == "export.series_name_yields_volume", series
    assert "rel_path" in actual["Dr Stone"] and "rel_path" in actual["Re Zero"]


@pytest.mark.req("EF-EXP-045")
def test_refusal_has_a_stable_catalog_code_and_the_suggestion() -> None:
    with pytest.raises(SeriesNameYieldsVolume) as caught:
        checked_export_path(FrozenAttrs("Re Zero S2", "main", "1"), simulate_parse)
    assert "exported series name" in str(caught.value)
    assert caught.value.code in {c.code for c in export_errors.CODES}


@pytest.mark.req("EF-EXP-045")
def test_a_closed_volume_may_carry_its_volume_but_not_its_folder() -> None:
    ok = checked_export_path(FrozenAttrs("Dr Stone", "main", "5", kind="volume"), simulate_parse)
    assert ok.filename == "Dr Stone - Vol.5.cbz"
    with pytest.raises(SeriesNameYieldsVolume):
        checked_export_path(FrozenAttrs("Dr Stone S2", "main", "5", kind="volume"), simulate_parse)
