"""Properties of the naming rules (EF-EXP-013, EF-EXP-061, EF-EXP-045)."""

import unicodedata

import pytest
import sqlalchemy as sa
from hypothesis import example, given, settings
from hypothesis import strategies as st

from shelfhold.export.domain.naming import (
    FrozenAttrs,
    export_path,
    find_number_collisions,
    sanitize_name,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from tests.export_env import export_env
from tests.fakes.kavita.plugin_parse import simulate_parse

FORBIDDEN = set('/\\:*?"<>|')
RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


@pytest.mark.req("EF-EXP-013")
@example("con", 200)
@example("nul", 200)
@example("..", 200)
@example("COM¹", 200)
@example("LPT¹", 200)
@example("CONIN$", 200)
@example("CONOUT$", 200)
@example(".hidden", 200)
@example("a\u200bb\u2028c\u2029d", 200)
@example("\u2028", 200)
@example("\u200b", 200)
@example("con     x", 200)
@example("CON.txt", 200)
@example("COM².cbz", 200)
@given(st.text(max_size=400), st.integers(min_value=8, max_value=255))
def test_sanitize_names_property(text: str, max_bytes: int) -> None:
    name = sanitize_name(text, max_bytes=max_bytes)
    assert name  # never empty
    assert not FORBIDDEN & set(name)
    assert all(unicodedata.category(c) not in ("Cc", "Cs") for c in name)
    assert name == unicodedata.normalize("NFC", name)
    assert name == name.strip() and not name.endswith(".")
    assert len(name.encode("utf-8")) <= max_bytes  # ext4: 255 bytes per component
    assert name.split(".")[0].strip().casefold() not in RESERVED
    assert not name.startswith(".")  # SMB / Windows device names
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
@settings(max_examples=12, deadline=None)
@given(
    st.lists(st.integers(1, 200), min_size=1, max_size=8, unique=True),
    st.lists(st.integers(1, 30), min_size=8, max_size=8),
)
def test_late_volume_attribution_never_renames(numbers: list[int], volumes: list[int]) -> None:
    """Chapters exported, then the source attributes tomes and corrects titles: the real
    pipeline plans no operation, so no `rename`; the sidecar change is only announced."""
    with export_env() as env:
        series = env.new_series()
        units = [env.add_chapter(series, str(n)) for n in numbers]
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        files = {p.name: p.stat().st_ino for p in env.files()}
        with env.engine.begin() as connection:
            for n, unit in enumerate(units):
                volume = new_ulid()
                connection.execute(
                    sa.insert(catalog.volume).values(
                        id=volume, edition_id=series.edition_id, number=str(volumes[n])
                    )
                )
                connection.execute(
                    sa.update(catalog.unit)
                    .where(catalog.unit.c.id == unit)
                    .values(volume_id=volume, title=f"Titre {n}")
                )
        preview = env.pipeline.preview(env.destination_id)
        assert preview.ops == ()
        assert {w.code for w in preview.warnings} == {"sidecar_changed"}
        assert env.pipeline.run(env.destination_id, job_id=new_ulid()).ops == ()
        assert {p.name: p.stat().st_ino for p in env.files()} == files
        with env.engine.connect() as connection:
            ops = set(connection.execute(sa.text("SELECT op FROM fs_intent")).scalars())
        assert "rename" not in ops


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


@pytest.mark.req("EF-EXP-013")
@pytest.mark.parametrize("name", ["COM\u00b9", "lpt\u00b3", "CONIN$", "conout$", "Con.txt"])
def test_sanitize_names_refuses_every_device_name(name: str) -> None:
    from shelfhold.export.domain.naming import is_reserved_name

    assert is_reserved_name(name)
    assert not is_reserved_name(sanitize_name(name))
