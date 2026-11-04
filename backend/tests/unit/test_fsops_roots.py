"""EF-EXP-029, EF-EXP-059, ENF-OPS-013: the runtime gate of every write, per physical root."""

import json
import shutil
from pathlib import Path

import pytest
from shelfhold.export.domain.physical_roots import RootState

from shelfhold.infrastructure.fsops import (
    Fsops,
    RootBlockedError,
    SentinelFormatError,
    Strategy,
)
from tests.fsops_env import Env, make_env


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path, generation=3)


def set_sentinel(root: Path, **changes: object) -> None:
    path = root / ".shelfhold-mount-id"
    content = json.loads(path.read_text(encoding="utf-8"))
    content.update(changes)
    path.write_text(json.dumps(content), encoding="utf-8")


def assert_blocked(env: Env, root: Path, state: RootState) -> None:
    probe = root / "X" / "1.cbz"
    with pytest.raises(RootBlockedError) as caught:
        env.fsops.write_atomic(str(probe), b"x", parents=True)
    assert caught.value.state is state
    assert caught.value.root_path == str(root)
    assert not probe.exists()
    assert not (root / "X").exists()


@pytest.mark.req("EF-EXP-029")
def test_sentinel_missing_blocks_reconcile(env: Env) -> None:
    (env.library / ".shelfhold-mount-id").unlink()
    with pytest.raises(RootBlockedError) as caught:
        env.fsops.require_scan_allowed(str(env.manga))
    assert caught.value.state is RootState.MISSING
    assert env.fsops.status(str(env.manga)).state is RootState.MISSING
    assert_blocked(env, env.library, RootState.MISSING)  # via manga: same root
    with pytest.raises(RootBlockedError):
        env.fsops.require_delete_allowed(str(env.manga / "X"))


@pytest.mark.req("EF-EXP-029")
@pytest.mark.parametrize("field", ["instance_id", "root_id"])
def test_sentinel_different_blocks_deletions_and_scans(env: Env, field: str) -> None:
    victim = env.manga / "X" / "1.cbz"
    env.fsops.write_atomic(str(victim), b"keep", parents=True)
    set_sentinel(env.library, **{field: "01ARZ3NDEKTSV4RRFFQ69G5FAV"})

    for call in (
        lambda: env.fsops.unlink(str(victim)),
        lambda: env.fsops.recycle(str(victim)),
        lambda: env.fsops.require_scan_allowed(str(env.manga)),
        lambda: env.fsops.require_delete_allowed(str(victim)),
    ):
        with pytest.raises(RootBlockedError) as caught:
            call()
        assert caught.value.state is RootState.MISMATCH
    assert victim.read_bytes() == b"keep"
    # Reading is not a write: the foreign tree stays readable for diagnostics.
    assert env.fsops.read_bytes(str(victim)) == b"keep"


@pytest.mark.req("EF-EXP-029")
def test_stale_generation_sentinel_blocks_writes(tmp_path: Path) -> None:
    env = make_env(tmp_path, generation=5)
    set_sentinel(env.library, generation=2)  # an old copy of the folder mounted by mistake
    assert_blocked(env, env.library, RootState.STALE)
    assert env.sentinel_of(env.library)["generation"] == 2  # never "fixed" automatically


@pytest.mark.req("EF-EXP-029")
def test_database_behind_the_sentinel_blocks_writes_as_restored_behind(tmp_path: Path) -> None:
    env = make_env(tmp_path, generation=5)
    set_sentinel(env.library, generation=9)
    assert_blocked(env, env.library, RootState.RESTORED_BEHIND)
    assert env.sentinel_of(env.library)["generation"] == 9


@pytest.mark.req("EF-EXP-029")
def test_generation_db_one_ahead_of_sentinel_is_ok(tmp_path: Path) -> None:
    env = make_env(tmp_path, generation=5)
    set_sentinel(env.library, generation=4)
    report = env.fsops.boot()
    assert report.restamped == [str(env.library)]
    assert env.fsops.status(str(env.manga)).state is RootState.OK
    assert env.sentinel_of(env.library)["generation"] == 5  # re-stamped
    env.fsops.write_atomic(str(env.manga / "ok.cbz"), b"x")


@pytest.mark.req("EF-EXP-029")
def test_write_gate_rereads_the_sentinel_before_every_write(env: Env) -> None:
    env.fsops.write_atomic(str(env.manga / "a.cbz"), b"x")
    (env.library / ".shelfhold-mount-id").unlink()  # the disk went away between two writes
    with pytest.raises(RootBlockedError):
        env.fsops.write_atomic(str(env.manga / "b.cbz"), b"x")


@pytest.mark.req("EF-EXP-029")
def test_stamp_after_commit_rewrites_the_generation_atomically(env: Env) -> None:
    env.fsops.stamp_sentinel(str(env.library), generation=4)
    assert env.sentinel_of(env.library)["generation"] == 4
    assert env.fsops.status(str(env.manga)).state is RootState.OK
    assert list((env.library / ".shelfhold" / "staging").iterdir()) == []


@pytest.mark.req("ENF-OPS-013")
def test_sentinel_m1b_fields_preserved_by_m1a_rewrite(env: Env) -> None:
    set_sentinel(
        env.library,
        schema_version="1.7",
        tombstone_count=3,
        tombstone_last_tag="t",
        tombstone_kids=["k1"],
        predecessor_instance_ids=["01ARZ3NDEKTSV4RRFFQ69G5FAV"],
        future={"x": [1]},
    )
    before = env.sentinel_of(env.library)
    env.fsops.stamp_sentinel(str(env.library), generation=4)
    after = env.sentinel_of(env.library)
    assert after["generation"] == 4
    assert {k: v for k, v in after.items() if k != "generation"} == {
        k: v for k, v in before.items() if k != "generation"
    }


@pytest.mark.req("ENF-OPS-013")
def test_newer_major_sentinel_is_never_rewritten(env: Env) -> None:
    set_sentinel(env.library, schema_version="2.0", brand_new={"a": 1})
    before = (env.library / ".shelfhold-mount-id").read_bytes()
    with pytest.raises(SentinelFormatError):
        env.fsops.stamp_sentinel(str(env.library), generation=4)
    assert (env.library / ".shelfhold-mount-id").read_bytes() == before
    with pytest.raises(RootBlockedError):
        env.fsops.write_atomic(str(env.manga / "x.cbz"), b"x")


@pytest.mark.req("EF-EXP-029")
def test_garbage_sentinel_blocks_like_a_different_one(env: Env) -> None:
    (env.library / ".shelfhold-mount-id").write_bytes(b"\x00garbage")
    assert_blocked(env, env.library, RootState.MISMATCH)


@pytest.mark.req("EF-EXP-029")
def test_sentinel_symlink_is_not_followed(env: Env, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.write_bytes((env.library / ".shelfhold-mount-id").read_bytes())
    (env.library / ".shelfhold-mount-id").unlink()
    (env.library / ".shelfhold-mount-id").symlink_to(other)
    assert_blocked(env, env.library, RootState.MISSING)


@pytest.mark.req("EF-EXP-059")
def test_missing_physical_root_blocks_only_its_writes(env: Env) -> None:
    shutil.rmtree(env.books)  # the sub-disk is gone; /library/books is not even a directory
    env.books.mkdir()  # ... or an empty mount point that looks like an emptied library
    assert_blocked(env, env.books, RootState.MISSING)
    with pytest.raises(RootBlockedError):
        env.fsops.require_scan_allowed(str(env.books))
    # The other root goes on.
    env.fsops.write_atomic(str(env.manga / "X" / "1.cbz"), b"ok", parents=True)
    assert (env.manga / "X" / "1.cbz").read_bytes() == b"ok"
    assert env.fsops.status(str(env.manga)).state is RootState.OK
    assert not (env.books / ".shelfhold").exists()  # nothing was created in the empty folder


@pytest.mark.req("EF-EXP-059")
def test_unmounted_subdisk_that_is_a_missing_directory_is_blocked_not_created(env: Env) -> None:
    shutil.rmtree(env.books)
    with pytest.raises(RootBlockedError) as caught:
        env.fsops.write_atomic(str(env.books / "Y" / "1.epub"), b"x", parents=True)
    assert caught.value.state is RootState.MISSING
    assert not env.books.exists()


@pytest.mark.req("ENF-OPS-014", "EF-EXP-029")
def test_mount_missing_blocks_all_writes(env: Env) -> None:
    shutil.rmtree(env.library)  # the whole /library is not mounted
    for root in (env.manga, env.comics, env.books):
        with pytest.raises(RootBlockedError):
            env.fsops.write_atomic(str(root / "X" / "1.cbz"), b"x", parents=True)
        with pytest.raises(RootBlockedError):
            env.fsops.mkdir(str(root / "Y"))
    assert not env.library.exists()
    report = env.fsops.boot()
    assert set(report.blocked) == {str(env.library), str(env.books)}
    assert not env.library.exists()


@pytest.mark.req("EF-EXP-059")
def test_boot_reports_each_root_independently(env: Env) -> None:
    (env.books / ".shelfhold-mount-id").unlink()
    report = env.fsops.boot()
    assert report.blocked == {str(env.books): RootState.MISSING}
    assert report.ok == [str(env.library)]


@pytest.mark.req("EF-EXP-006", "ENF-OPS-011")
def test_orphan_staging_purged_at_boot(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    for root in (env.library, env.books):
        staging = root / ".shelfhold" / "staging"
        staging.mkdir(parents=True)
        (staging / "0f0f").write_bytes(b"half a file")
        (staging / "dir").mkdir()
        (staging / "dir" / "inner").write_bytes(b"x")
    recycle = env.library / ".shelfhold" / "recycle"
    recycle.mkdir()
    (recycle / "kept").write_bytes(b"recycled")
    manga_file = env.manga / "real.cbz"
    manga_file.write_bytes(b"real")

    report = Fsops.open(env.path_map, env.expected).boot()

    assert report.purged_staging == 4
    assert list((env.library / ".shelfhold" / "staging").iterdir()) == []
    assert list((env.books / ".shelfhold" / "staging").iterdir()) == []
    assert (recycle / "kept").read_bytes() == b"recycled"
    assert manga_file.read_bytes() == b"real"


@pytest.mark.req("EF-EXP-006", "EF-EXP-059")
def test_orphan_staging_of_a_blocked_root_is_left_alone(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    staging = env.books / ".shelfhold" / "staging"
    staging.mkdir(parents=True)
    (staging / "orphan").write_bytes(b"x")
    set_sentinel(env.books, instance_id="01ARZ3NDEKTSV4RRFFQ69G5FAV")
    report = env.fsops.boot()
    assert report.purged_staging == 0
    assert (staging / "orphan").exists()


@pytest.mark.req("EF-EXP-006")
def test_boot_creates_staging_and_recycle_only_in_verified_rw_roots(env: Env) -> None:
    env.fsops.boot()
    assert (env.library / ".shelfhold" / "staging").is_dir()
    assert (env.library / ".shelfhold" / "recycle").is_dir()
    assert (env.books / ".shelfhold" / "staging").is_dir()
    assert not (env.upstream / ".shelfhold").exists()


@pytest.mark.req("EF-EXP-059")
def test_path_outside_the_path_map_is_refused_even_under_a_root(env: Env) -> None:
    from shelfhold.infrastructure.fsops import OutsidePathMapError

    with pytest.raises(OutsidePathMapError):
        env.fsops.write_atomic(str(env.library / "stray.cbz"), b"x")
    with pytest.raises(OutsidePathMapError):
        env.fsops.write_atomic(str(env.tmp / "elsewhere.cbz"), b"x")
    assert not (env.library / "stray.cbz").exists()


@pytest.mark.req("EF-EXP-059")
def test_strategies_give_the_same_gate(tmp_path: Path) -> None:
    for index, strategy in enumerate([Strategy.OPENAT2, Strategy.FALLBACK]):
        env = make_env(tmp_path / str(index), strategy=strategy)
        (env.books / ".shelfhold-mount-id").unlink()
        with pytest.raises(RootBlockedError):
            env.fsops.write_atomic(str(env.books / "x"), b"x")
        env.fsops.write_atomic(str(env.manga / "x"), b"x")
