"""EF-BIB-054 (2)-(4), ENF-OPS-021: membership across identity migrations, cited versions kept."""

from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shelfhold.catalog.domain.numbering import parse_number
from shelfhold.catalog.public import NormalizerRegistry, default_registry
from shelfhold.library.tombstones import (
    CITABLE_NORMALIZERS,
    CitedVersionMissingError,
    ContentHash,
    MembershipIndex,
    PlaceRef,
    VariantRef,
    missing_citable,
    normalizers_of,
)
from tests.tombstone_env import PLACE, VARIANT, make_tomb_env

pytestmark = pytest.mark.req("EF-BIB-054", "ENF-OPS-021")


def _with_number_v2(fn: Callable[[str], str]) -> NormalizerRegistry:
    registry = default_registry()
    registry.register("number", 2, fn)
    return registry


def _strict(raw: str) -> str:
    """A stricter number@2: "7.0" stays "7.0" (it was "7" under number@1)."""
    return raw.strip()


@pytest.mark.req("ENF-OPS-021")
def test_cited_normalizer_version_never_removed() -> None:
    """Every version a tombstone may cite is still in the code; removing one fails here, and the
    index refuses to build over a cited version it does not have."""
    assert missing_citable(default_registry()) == []
    assert set(CITABLE_NORMALIZERS) >= {"number@1", "lang@1"}
    without_number = NormalizerRegistry()
    for name in ("series_name", "team_label", "text_version", "lang"):
        without_number.register(name, 1, str)
    assert "number@1" in missing_citable(without_number)
    assert missing_citable(default_registry(), content_hashes=frozenset()) == ["content-hash@1"]
    index = MembershipIndex(normalizers_of(without_number))
    with pytest.raises(CitedVersionMissingError) as missing:
        index.add_all([(PLACE, {"number": "number@1", "lang": "lang@1"})])
    assert missing.value.missing == ("number@1",)


@pytest.mark.parametrize(
    "migration", ["normalizer_v_next", "content_hash_v2", "edition_key_column_added"]
)
def test_tombstone_survives_every_identity_migration(tmp_path: Path, migration: str) -> None:
    """Purged under number@1 and content-hash@1; after the migration the same reference or file
    comes back: nothing is requested or stored (M2, M3, M6 cases come with their lots)."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    service.record(VARIANT, env.roots)
    service.record(PLACE, env.roots)
    migrated = env.service(normalizers=normalizers_of(_with_number_v2(_strict)))
    migrated.replay(env.roots)
    if migration == "normalizer_v_next":
        again = PlaceRef.of(dict(PLACE.edition_key), track="main", number_segment="",
                            number_raw="7.0", part="")  # fmt: skip
        assert migrated.check(again, root_id=env.library.root_id).blocked
    elif migration == "content_hash_v2":
        reuploaded = VariantRef(
            "suwayomi", "4422", "/chapter/new-upload-id",
            (ContentHash("content-hash@2", "sha256:" + "11" * 32),),
        )  # fmt: skip

        def hash_with(by: str) -> str | None:
            return VARIANT.content_hashes[0].value if by == "content-hash@1" else None

        verdict = migrated.check(reuploaded, root_id=env.library.root_id, hash_with=hash_with)
        assert verdict.blocked and verdict.cause == "tombstoned"
        assert not migrated.check(reuploaded, root_id=env.library.root_id).blocked
    else:
        wider = PlaceRef.of({**dict(PLACE.edition_key), "packaging": ""}, track="main",
                            number_segment="", number_raw="7", part="")  # fmt: skip
        assert migrated.check(wider, root_id=env.library.root_id).blocked
    different = PlaceRef.of(dict(PLACE.edition_key), track="main", number_segment="",
                            number_raw="8", part="")  # fmt: skip
    assert not migrated.check(different, root_id=env.library.root_id).blocked


def test_identity_migration_consults_tombstones(tmp_path: Path) -> None:
    """`normalize migrate`: a live row that, migrated, would match a tombstone is blocked and
    `tombstone.migration_hit` is emitted."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    purged = PlaceRef.of(dict(PLACE.edition_key), track="main", number_segment="",
                         number_raw="7", part="")  # fmt: skip
    service.record(purged, env.roots)
    # number@2 reads "seven" as 7: the live row "seven" (unnumbered under number@1) would land
    # on the purged place once migrated.
    registry = _with_number_v2(lambda raw: "7" if raw == "seven" else parse_number(raw).norm)
    migrating = env.service(normalizers=normalizers_of(registry))
    migrating.replay(env.roots)
    live = PlaceRef.of(dict(PLACE.edition_key), track="main", number_segment="",
                       number_raw="seven", part="")  # fmt: skip
    untouched = PlaceRef.of(dict(PLACE.edition_key), track="main", number_segment="",
                            number_raw="12", part="")  # fmt: skip

    blocked = migrating.screen_migration([("row-1", live),
                                          ("row-2", untouched)])  # fmt: skip

    assert blocked == {"row-1"}
    assert env.events("tombstone.migration_hit") == [{"scope": "place"}]
    assert not service.check(live, root_id=None).blocked  # under number@1 alone: no match


@settings(max_examples=80)
@given(number=st.integers(0, 500), fraction=st.sampled_from(["", ".0", ".00"]))
def test_tombstoned_number_blocked_under_every_cited_version(number: int, fraction: str) -> None:
    """Property: a place purged under number@1 stays blocked for every spelling number@1 merges
    with it, whatever number@2 says."""
    index = MembershipIndex(normalizers_of(_with_number_v2(_strict)))
    place = PlaceRef.of({"work_id": "W1", "lang": "fr"}, track="main", number_segment="",
                        number_raw=str(number), part="")  # fmt: skip
    index.add_all([(place, {"number": "number@1", "lang": "lang@1"})])
    spelled = PlaceRef.of({"work_id": "W1", "lang": "FR"}, track="main", number_segment="",
                          number_raw=f"{number:03d}{fraction}", part="")  # fmt: skip
    assert index.contains(spelled)
