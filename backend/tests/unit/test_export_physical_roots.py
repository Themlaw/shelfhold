"""EF-EXP-029, EF-EXP-059, ENF-OPS-013: sentinel judgement and physical roots, as pure rules."""

import json

import pytest
from ulid import ULID

from shelfhold.export.domain.path_map import PathEntry, PathMap, PathMode
from shelfhold.export.domain.physical_roots import (
    SENTINEL_FILE,
    ExpectedRoot,
    RootState,
    SentinelDocument,
    SentinelFormatError,
    discover_physical_roots,
    is_reserved,
    judge_sentinel,
)

ROOT, INSTANCE = str(ULID()), str(ULID())


def expected(generation: int) -> ExpectedRoot:
    return ExpectedRoot(ROOT, "/library", INSTANCE, generation)


def doc(generation: int, *, root_id: str = ROOT, instance_id: str = INSTANCE) -> SentinelDocument:
    return SentinelDocument(root_id=root_id, instance_id=instance_id, generation=generation)


@pytest.mark.req("EF-EXP-029")
def test_sentinel_missing_blocks_reconcile() -> None:
    verdict = judge_sentinel(None, expected(3))
    assert verdict.state is RootState.MISSING
    assert verdict.blocks_writes
    assert verdict.blocks_scans_and_deletions


@pytest.mark.req("EF-EXP-029")
@pytest.mark.parametrize(
    "other",
    [doc(3, instance_id=str(ULID())), doc(3, root_id=str(ULID()))],
    ids=["other-instance", "other-root"],
)
def test_sentinel_different_blocks_deletions_and_scans(other: SentinelDocument) -> None:
    verdict = judge_sentinel(other, expected(3))
    assert verdict.state is RootState.MISMATCH
    assert verdict.blocks_writes
    assert verdict.blocks_scans_and_deletions


@pytest.mark.req("EF-EXP-029")
def test_stale_generation_sentinel_blocks_writes() -> None:
    # An old copy of the folder: the sentinel is more than one step behind the database.
    verdict = judge_sentinel(doc(2), expected(5))
    assert verdict.state is RootState.STALE
    assert verdict.blocks_writes
    assert not verdict.restamp


@pytest.mark.req("EF-EXP-029")
def test_generation_db_one_ahead_of_sentinel_is_ok() -> None:
    # Crash between the commit and the rewrite of the sentinel: normal, sentinel re-stamped.
    verdict = judge_sentinel(doc(4), expected(5))
    assert verdict.state is RootState.OK
    assert verdict.restamp
    assert not verdict.blocks_writes


@pytest.mark.req("EF-EXP-029")
def test_equal_generation_is_ok_without_restamp() -> None:
    verdict = judge_sentinel(doc(5), expected(5))
    assert (verdict.state, verdict.restamp) == (RootState.OK, False)


@pytest.mark.req("EF-EXP-029")
def test_database_behind_the_sentinel_is_restored_behind_not_ok() -> None:
    verdict = judge_sentinel(doc(7), expected(5))
    assert verdict.state is RootState.RESTORED_BEHIND
    assert verdict.blocks_writes
    assert not verdict.restamp  # only `reconcile --adopt-after-restore` re-stamps


@pytest.mark.req("EF-EXP-029")
def test_generation_states_are_the_persisted_vocabulary() -> None:
    from shelfhold.infrastructure.schema.platform import SENTINEL_STATES

    persisted = {state.value for state in RootState if state is not RootState.RESTORED_BEHIND}
    assert persisted == set(SENTINEL_STATES)


@pytest.mark.req("ENF-OPS-013")
def test_sentinel_m1b_fields_preserved_by_m1a_rewrite() -> None:
    raw = json.dumps(
        {
            "schema_version": "1.4",
            "root_id": ROOT,
            "instance_id": INSTANCE,
            "generation": 4,
            "tombstone_count": 12,
            "tombstone_last_tag": "abc",
            "tombstone_kids": ["k1", "k2"],
            "predecessor_instance_ids": [str(ULID())],
            "unknown_future": {"nested": [1, 2]},
        }
    ).encode()
    parsed = SentinelDocument.parse(raw)
    rewritten = json.loads(parsed.restamped(5).dumps())
    original = json.loads(raw)
    assert rewritten["generation"] == 5
    assert rewritten["schema_version"] == "1.4"  # a newer minor is never downgraded
    for key in set(original) - {"generation"}:
        assert rewritten[key] == original[key], key


@pytest.mark.req("ENF-OPS-013")
def test_sentinel_without_version_is_written_with_the_current_one() -> None:
    fresh = json.loads(doc(0).dumps())
    assert fresh["schema_version"].count(".") == 1
    assert set(fresh) >= {"root_id", "instance_id", "generation"}


@pytest.mark.req("ENF-OPS-013")
def test_newer_major_sentinel_is_read_only_and_blocks_writes() -> None:
    raw = json.dumps(
        {
            "schema_version": "2.0",
            "root_id": ROOT,
            "instance_id": INSTANCE,
            "generation": 5,
        }
    ).encode()
    parsed = SentinelDocument.parse(raw)
    assert parsed.read_only
    with pytest.raises(SentinelFormatError):
        parsed.restamped(6)
    verdict = judge_sentinel(parsed, expected(5))
    assert verdict.blocks_writes


@pytest.mark.req("ENF-OPS-013")
@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[]", b'{"root_id": 1}', b'{"schema_version": "x"}'],
)
def test_garbage_sentinel_is_a_format_error(raw: bytes) -> None:
    with pytest.raises(SentinelFormatError):
        SentinelDocument.parse(raw)


@pytest.mark.req("EF-EXP-059")
def test_library_subdir_on_other_device_uses_local_staging_and_sentinel() -> None:
    """Pure part: /library/books is on another device, so it is a root of its own."""
    path_map = PathMap(
        (
            PathEntry("/library/manga", "/manga"),
            PathEntry("/library/comics", "/comics"),
            PathEntry("/library/books", "/books"),
        )
    )
    devices = {"/library": 10, "/library/manga": 10, "/library/comics": 10, "/library/books": 20}
    roots = discover_physical_roots(path_map, "/library", devices.__getitem__)
    by_path = {r.path: r for r in roots}
    assert set(by_path) == {"/library", "/library/books"}
    assert {e.local for e in by_path["/library"].entries} == {"/library/manga", "/library/comics"}
    assert [e.local for e in by_path["/library/books"].entries] == ["/library/books"]
    assert by_path["/library"].st_dev == 10
    assert by_path["/library/books"].st_dev == 20


@pytest.mark.req("EF-EXP-059", "EF-EXP-060")
def test_adopt_ro_volume_outside_library_is_a_root_of_its_own() -> None:
    path_map = PathMap(
        (
            PathEntry("/library/manga", "/manga"),
            PathEntry("/upstream/suwayomi", "/suwayomi", PathMode.ADOPT_RO),
        )
    )
    devices = {"/library": 1, "/library/manga": 1, "/upstream/suwayomi": 9}
    roots = discover_physical_roots(path_map, "/library", devices.__getitem__)
    assert {r.path for r in roots} == {"/library", "/upstream/suwayomi"}


@pytest.mark.req("EF-EXP-059")
def test_absent_entry_is_reported_as_its_own_absent_root() -> None:
    path_map = PathMap((PathEntry("/library/manga", "/manga"), PathEntry("/library/b", "/b")))

    def dev_of(path: str) -> int:
        if path == "/library/b":
            raise FileNotFoundError(path)
        return 1

    roots = discover_physical_roots(path_map, "/library", dev_of)
    absent = [r for r in roots if r.st_dev is None]
    assert [r.path for r in absent] == ["/library/b"]


@pytest.mark.req("EF-EXP-060", "ENF-SEC-012")
@pytest.mark.parametrize(
    "rel",
    [
        SENTINEL_FILE,
        ".shelfhold",
        ".shelfhold/staging/x",
        "./.shelfhold/recycle",
        ".shelfhold//lease",
        "a/../.shelfhold/manifest",
        ".shelfhold/",
        "./.shelfhold-mount-id",
    ],
)
def test_reserved_paths_are_recognised_lexically(rel: str) -> None:
    assert is_reserved(rel)


@pytest.mark.req("EF-EXP-060")
@pytest.mark.parametrize(
    "rel", ["manga/.shelfhold", "manga/X/.shelfhold-mount-id", ".shelfholdx/a", "shelfhold", "a"]
)
def test_only_the_root_level_names_are_reserved(rel: str) -> None:
    assert not is_reserved(rel)
