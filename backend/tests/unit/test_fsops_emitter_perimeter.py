"""EF-USR-024 (passe 13, N2): a system emitter deletes only inside its own perimeter, checked by fsops."""

from pathlib import Path

import pytest

from shelfhold.identity.public import SYSTEM_EMITTERS
from shelfhold.infrastructure.fsops import (
    EMITTER_PERIMETERS,
    EmitterPerimeterError,
    Strategy,
)
from tests.fsops_env import STRATEGIES, Env, make_env

PERIMETER_CASES = [
    ("staging_orphan_cleanup", ".shelfhold/staging/orphan.part"),
    ("probe_file_cleanup", "Series/.shelfhold-probe-3f9a"),
    ("trash_retention_purge", ".shelfhold/recycle/01JC/chapter.cbz"),
    ("disk_guard_purge", ".shelfhold/recycle/01JC/chapter.cbz"),
]
OUTSIDE = [
    "Series/1.cbz",
    ".shelfhold-mount-id",
    ".shelfhold/lease",
    ".shelfhold/tombstones",
    ".shelfhold/staging-lookalike/x",
    ".shelfhold/recycle-lookalike/x",
]


@pytest.fixture(params=STRATEGIES, ids=lambda s: s.value)
def env(request: pytest.FixtureRequest, tmp_path: Path) -> Env:
    strategy: Strategy = request.param
    return make_env(tmp_path, strategy=strategy)


def _touch(root: Path, rel: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


@pytest.mark.req("EF-USR-024")
def test_perimeters_exist_for_exactly_the_closed_list() -> None:
    assert set(EMITTER_PERIMETERS) == SYSTEM_EMITTERS


@pytest.mark.req("EF-USR-024")
@pytest.mark.parametrize(("emitter", "rel"), PERIMETER_CASES)
def test_a_system_emitter_deletes_inside_its_perimeter(env: Env, emitter: str, rel: str) -> None:
    path = _touch(env.manga, rel)
    env.fsops.system_unlink(emitter, str(path))
    assert not path.exists()


@pytest.mark.req("EF-USR-024")
@pytest.mark.parametrize("emitter", sorted(SYSTEM_EMITTERS))
@pytest.mark.parametrize("rel", OUTSIDE)
def test_a_system_emitter_cannot_delete_outside_its_perimeter(
    env: Env, emitter: str, rel: str
) -> None:
    path = _touch(env.manga, rel)
    inside = {e: r for e, r in PERIMETER_CASES}
    if rel == inside.get(emitter):
        pytest.skip("inside")
    with pytest.raises(EmitterPerimeterError):
        env.fsops.system_unlink(emitter, str(path))
    assert path.exists()


@pytest.mark.req("EF-USR-024")
def test_probe_names_are_confined_to_the_probe_pattern_and_never_to_reserved_dirs(env: Env) -> None:
    inside_reserved = _touch(env.manga, ".shelfhold/staging/.shelfhold-probe-1")
    with pytest.raises(EmitterPerimeterError):
        env.fsops.system_unlink("probe_file_cleanup", str(inside_reserved))
    lookalike = _touch(env.manga, "Series/.shelfhold-probe")  # no suffix: not a probe file
    with pytest.raises(EmitterPerimeterError):
        env.fsops.system_unlink("probe_file_cleanup", str(lookalike))


@pytest.mark.req("EF-USR-024")
def test_unknown_emitter_is_refused(env: Env) -> None:
    path = _touch(env.manga, ".shelfhold/staging/x")
    with pytest.raises(EmitterPerimeterError):
        env.fsops.system_unlink("cleanup_everything", str(path))
    assert path.exists()


@pytest.mark.req("EF-USR-024")
def test_a_link_inside_the_perimeter_is_removed_never_followed(env: Env, tmp_path: Path) -> None:
    victim = _touch(env.manga, "Series/precious.cbz")
    link = env.manga / ".shelfhold" / "staging" / "evil"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(victim)
    env.fsops.system_unlink("staging_orphan_cleanup", str(link))
    assert not link.is_symlink()
    assert victim.exists()


@pytest.mark.req("EF-USR-024")
def test_a_directory_tree_is_removed_only_inside_the_perimeter(env: Env) -> None:
    tree = env.manga / ".shelfhold" / "recycle" / "01JC"
    _touch(tree, "a/b.cbz")
    env.fsops.system_remove_tree("trash_retention_purge", str(tree))
    assert not tree.exists()
    other = _touch(env.manga, "Series/Vol 1/1.cbz").parent
    with pytest.raises(EmitterPerimeterError):
        env.fsops.system_remove_tree("trash_retention_purge", str(other))
    assert other.exists()


@pytest.mark.req("EF-USR-024")
def test_the_boot_staging_purge_runs_under_its_emitter(env: Env) -> None:
    orphan = _touch(env.manga, ".shelfhold/staging/half.part")
    env.fsops.boot()
    assert not orphan.exists()
