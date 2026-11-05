"""Properties of the naming rules (EF-EXP-013, EF-EXP-061, EF-EXP-045)."""

import unicodedata

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shelfhold.export.domain.naming import (
    FrozenAttrs,
    PlannedItem,
    export_path,
    find_number_collisions,
    plan_renames,
    sanitize_name,
)
from tests.fakes.kavita.plugin_parse import simulate_parse

FORBIDDEN = set('/\\:*?"<>|')
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


@pytest.mark.req("EF-EXP-013")
@given(st.text(max_size=400), st.integers(min_value=8, max_value=255))
def test_sanitize_names_property(text: str, max_bytes: int) -> None:
    name = sanitize_name(text, max_bytes=max_bytes)
    assert name  # never empty
    assert not FORBIDDEN & set(name)
    assert all(unicodedata.category(c) not in ("Cc", "Cs") for c in name)
    assert name == unicodedata.normalize("NFC", name)
    assert name == name.strip() and not name.endswith(".")
    assert len(name.encode("utf-8")) <= max_bytes  # ext4: 255 bytes per component
    assert name.split(".")[0].lower() not in RESERVED  # SMB / Windows device names
    assert sanitize_name(name, max_bytes=max_bytes) == name  # idempotent


numbers = st.one_of(
    st.integers(0, 3000).map(str),
    st.tuples(st.integers(0, 3000), st.integers(1, 9)).map(lambda t: f"{t[0]}.{t[1]}"),
)
series_names = st.text(alphabet="ABCDEFGHIJ klmnop:?'", min_size=1, max_size=30).filter(
    lambda s: s.strip(" .") != ""
)


@st.composite
def frozen_items(draw: st.DrawFn) -> FrozenAttrs:
    return FrozenAttrs(
        series=draw(series_names),
        track=draw(st.sampled_from(["main", "main", "main", "annual", "special", "extra"])),
        number_norm=draw(numbers),
        segment=draw(st.sampled_from(["", "", "S2", "S3"])),
    )


@pytest.mark.req("EF-EXP-061")
@settings(max_examples=60)
@given(
    st.lists(frozen_items(), min_size=1, max_size=40),
    st.lists(st.one_of(st.none(), st.integers(1, 80)), min_size=40, max_size=40),
)
def test_late_volume_attribution_never_renames(
    items: list[FrozenAttrs], volumes: list[int | None]
) -> None:
    """A tome attributed late (or a corrected title) changes the metadata, never the path."""
    before = [PlannedItem(f"i{n}", f) for n, f in enumerate(items)]
    recorded = {p.item_id: export_path(p.frozen).rel_path for p in before}
    after = [
        PlannedItem(p.item_id, p.frozen, current_volume=volumes[n], current_title=f"Titre {n}")
        for n, p in enumerate(before)
    ]
    assert plan_renames(recorded, after) == []


@pytest.mark.req("EF-EXP-045")
@settings(max_examples=60)
@given(st.lists(numbers, min_size=1, max_size=40, unique=True), series_names)
def test_no_two_files_same_number_in_series_dir_property(nums: list[str], series: str) -> None:
    """Distinct places of one series give distinct parsed numbers (naming side of the rule)."""
    names = [
        export_path(FrozenAttrs(series=series, track="main", number_norm=n)).filename
        for n in nums
        if float(n) != 0.0
    ]
    distinct = {float(n) for n in nums if float(n) != 0.0}
    assert len(distinct) == len(names)
    assert find_number_collisions(names, simulate_parse) == []
