"""EF-USR-024 (N2): a system emitter deletes only inside its perimeter and while its condition
holds, both checked by fsops. Includes the regressions of the PR #43 review PoCs."""

import os
from pathlib import Path

import pytest

from shelfhold.identity.public import SYSTEM_EMITTERS
from shelfhold.infrastructure.fsops import (
    EMITTER_PERIMETERS,
    EmitterConditionError,
    EmitterPerimeterError,
    Fsops,
    FsopsError,
    Strategy,
    new_probe_name,
)
from tests.fsops_env import STRATEGIES, Env, make_env

PROBE = ".shelfhold-probe-0123456789abcdef"
NOW = 2_000_000_000.0
DAY = 86400
PERIMETER_CASES = [
    ("staging_orphan_cleanup", ".shelfhold/staging/orphan.part"),
    ("probe_file_cleanup", f"manga/Series/{PROBE}"),
    ("trash_retention_purge", ".shelfhold/recycle/01JC/chapter.cbz"),
]
OUTSIDE = [
    "manga/Series/1.cbz",
    ".shelfhold-mount-id",
    ".shelfhold/lease",
    ".shelfhold/tombstones",
    ".shelfhold/staging-lookalike/x",
    ".shelfhold/recycle-lookalike/x",
]


@pytest.fixture(params=STRATEGIES, ids=lambda s: s.value)
def env(request: pytest.FixtureRequest, tmp_path: Path) -> Env:
    strategy: Strategy = request.param
    made = make_env(tmp_path, strategy=strategy)
    # The trash is 31 days old and the disk guard is triggered, unless a test says otherwise.
    made.fsops = Fsops.open(made.path_map, made.expected, strategy=strategy, now=lambda: NOW)
    return made


def _touch(root: Path, rel: str, *, age_days: float = 31) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    age = NOW - age_days * DAY
    for folder in [*path.parents][: len(Path(rel).parts) - 1]:
        os.utime(folder, (age, age))
    os.utime(path, (age, age))
    return path


@pytest.mark.req("EF-USR-024")
def test_perimeters_exist_for_exactly_the_closed_list() -> None:
    assert set(EMITTER_PERIMETERS) == SYSTEM_EMITTERS


@pytest.mark.req("EF-USR-024")
@pytest.mark.parametrize(("emitter", "rel"), PERIMETER_CASES)
def test_a_system_emitter_deletes_inside_its_perimeter(env: Env, emitter: str, rel: str) -> None:
    path = _touch(env.library, rel)
    env.fsops.system_unlink(emitter, str(path))
    assert not path.exists()


@pytest.mark.req("EF-USR-024")
@pytest.mark.parametrize("emitter", sorted(SYSTEM_EMITTERS))
@pytest.mark.parametrize("rel", OUTSIDE)
def test_a_system_emitter_cannot_delete_outside_its_perimeter(
    env: Env, emitter: str, rel: str
) -> None:
    path = _touch(env.library, rel)
    with pytest.raises(EmitterPerimeterError):
        env.fsops.system_unlink(emitter, str(path))
    assert path.exists()


@pytest.mark.req("EF-USR-024")
def test_unknown_emitter_is_refused(env: Env) -> None:
    path = _touch(env.library, ".shelfhold/staging/x")
    with pytest.raises(EmitterPerimeterError):
        env.fsops.system_unlink("cleanup_everything", str(path))
    assert path.exists()


@pytest.mark.req("EF-USR-024")
def test_dotdot_is_cleaned_before_the_perimeter(env: Env) -> None:
    victim = _touch(env.library, "manga/Series/1.cbz")
    sneaky = env.library / ".shelfhold/staging/../../manga/Series/1.cbz"
    (env.library / ".shelfhold/staging").mkdir(parents=True, exist_ok=True)
    with pytest.raises(FsopsError):
        env.fsops.system_unlink("staging_orphan_cleanup", str(sneaky))
    assert victim.exists()


@pytest.mark.req("EF-USR-024")
def test_a_link_inside_the_perimeter_is_removed_never_followed(env: Env) -> None:
    victim = _touch(env.library, "manga/Series/precious.cbz")
    link = env.library / ".shelfhold" / "staging" / "evil"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(victim)
    env.fsops.system_unlink("staging_orphan_cleanup", str(link))
    assert not link.is_symlink()
    assert victim.exists()


@pytest.mark.req("EF-USR-024")
def test_a_linked_folder_in_staging_is_not_followed(env: Env) -> None:
    victim = _touch(env.library, "manga/Series/1.cbz")
    (env.library / ".shelfhold/staging").mkdir(parents=True, exist_ok=True)
    (env.library / ".shelfhold/staging/evil").symlink_to(env.library / "manga/Series")
    with pytest.raises((FsopsError, OSError)):
        env.fsops.system_unlink(
            "staging_orphan_cleanup", str(env.library / ".shelfhold/staging/evil/1.cbz")
        )
    assert victim.exists()


@pytest.mark.req("EF-USR-024")
def test_a_probe_through_a_linked_parent_is_refused(env: Env, tmp_path: Path) -> None:
    outside = _touch(tmp_path / "outside", PROBE)
    (env.library / "manga/lnk").symlink_to(outside.parent)
    with pytest.raises((FsopsError, OSError)):
        env.fsops.system_unlink("probe_file_cleanup", str(env.library / "manga/lnk" / PROBE))
    assert outside.exists()


# --- probe: a single regular file, the exact name the probe draws (review M3) -----------------


@pytest.mark.req("EF-USR-024")
def test_the_probe_name_is_the_one_the_probe_draws() -> None:
    name = new_probe_name()
    assert EMITTER_PERIMETERS["probe_file_cleanup"].allows(f"manga/{name}")
    assert name != new_probe_name()


@pytest.mark.req("EF-USR-024")
@pytest.mark.parametrize(
    "name", [".shelfhold-probe", ".shelfhold-probeX", ".shelfhold-probe-1", ".shelfhold-probe-zz"]
)
def test_lookalike_probe_names_are_refused(env: Env, name: str) -> None:
    path = _touch(env.library, f"manga/Series/{name}")
    with pytest.raises(EmitterPerimeterError):
        env.fsops.system_unlink("probe_file_cleanup", str(path))
    assert path.exists()


@pytest.mark.req("EF-USR-024")
def test_the_probe_emitter_never_deletes_a_folder(env: Env) -> None:
    """Regression PoC q3: a user folder named like a probe went whole through remove_tree."""
    inside = _touch(env.library, f"manga/Other/{PROBE}/ch1.cbz")
    with pytest.raises(EmitterPerimeterError):
        env.fsops.system_remove_tree("probe_file_cleanup", str(inside.parent))
    with pytest.raises(EmitterPerimeterError):
        env.fsops.system_unlink("probe_file_cleanup", str(inside.parent))  # a folder, not a file
    assert inside.exists()


@pytest.mark.req("EF-USR-024")
def test_the_probe_emitter_stays_out_of_the_reserved_area(env: Env) -> None:
    inside_reserved = _touch(env.library, f".shelfhold/staging/{PROBE}")
    with pytest.raises(EmitterPerimeterError):
        env.fsops.system_unlink("probe_file_cleanup", str(inside_reserved))


# --- conditions (review blocker 1) -------------------------------------------------------------


@pytest.mark.req("EF-USR-024")
@pytest.mark.parametrize("emitter", ["trash_retention_purge"])
def test_the_trash_emitter_keeps_what_is_less_than_30_days_old(env: Env, emitter: str) -> None:
    """Regression PoC q1: an item trashed just now vanished without a reserved action."""
    fresh = _touch(env.library, ".shelfhold/recycle/01NOW/Series/Vol 1.cbz", age_days=29)
    with pytest.raises(EmitterConditionError):
        env.fsops.system_remove_tree(emitter, str(env.library / ".shelfhold/recycle/01NOW"))
    with pytest.raises(EmitterConditionError):
        env.fsops.system_unlink(emitter, str(fresh))
    assert fresh.exists()


@pytest.mark.req("EF-USR-024")
def test_the_trash_emitter_removes_what_is_30_days_old_or_more(env: Env) -> None:
    old = _touch(env.library, ".shelfhold/recycle/01OLD/Series/Vol 1.cbz", age_days=30.5)
    env.fsops.system_remove_tree(
        "trash_retention_purge", str(env.library / ".shelfhold/recycle/01OLD")
    )
    assert not old.exists()


@pytest.mark.req("EF-USR-024")
def test_the_boot_staging_purge_runs_under_its_emitter(env: Env) -> None:
    orphan = _touch(env.library, ".shelfhold/staging/half.part")
    env.fsops.boot()
    assert not orphan.exists()
