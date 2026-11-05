"""`Fsops` is the only writer of `<root>/.shelfhold/manifest/` (EF-EXP-063, EF-EXP-060, I37)."""

import stat
from pathlib import Path

import pytest

from shelfhold.export.domain.provenance_mirror import mirror_name
from shelfhold.infrastructure.fsops import (
    OutsidePathMapError,
    ReadOnlyRootError,
    ReservedPathError,
    RootBlockedError,
)
from shelfhold.infrastructure.ids import new_ulid
from tests.fsops_env import make_env

pytestmark = pytest.mark.req("EF-EXP-063")


def test_mirror_is_written_atomically_with_private_modes(tmp_path: Path) -> None:
    env = make_env(tmp_path, umask=0o022)
    name = mirror_name(new_ulid(), new_ulid())
    env.fsops.write_mirror(str(env.library), name, b'{"a": 1}\n')
    written = env.library / ".shelfhold" / "manifest" / name
    assert written.read_bytes() == b'{"a": 1}\n'
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert stat.S_IMODE(written.parent.stat().st_mode) == 0o700
    assert env.fsops.read_mirror(str(env.library), name) == b'{"a": 1}\n'
    assert env.fsops.list_mirror(str(env.library)) == [name]
    env.fsops.write_mirror(str(env.library), name, b"{}\n")  # replaced, never half-written
    assert env.fsops.read_mirror(str(env.library), name) == b"{}\n"


def test_no_mirror_on_adopt_ro_root(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    name = mirror_name(new_ulid(), new_ulid())
    with pytest.raises(ReadOnlyRootError):
        env.fsops.write_mirror(str(env.upstream), name, b"{}\n")
    assert not (env.upstream / ".shelfhold").exists()


def test_mirror_names_cannot_escape_the_manifest_folder(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    for bad in ("../lease", "x.jsonl", f"{new_ulid()}/../../lease", "/etc/passwd"):
        with pytest.raises(ReservedPathError):
            env.fsops.write_mirror(str(env.library), bad, b"{}\n")
    assert not (env.library / ".shelfhold" / "lease").exists()


def test_mirror_is_refused_on_a_path_that_is_not_a_root(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    with pytest.raises(OutsidePathMapError):
        env.fsops.write_mirror(str(env.manga), mirror_name(new_ulid(), new_ulid()), b"{}\n")


def test_mirror_is_refused_on_a_root_whose_sentinel_is_missing(tmp_path: Path) -> None:
    env = make_env(tmp_path, sentinels=("library",))  # `books` has no sentinel
    with pytest.raises(RootBlockedError):
        env.fsops.write_mirror(str(env.books), mirror_name(new_ulid(), new_ulid()), b"{}\n")


def test_mirror_write_goes_through_the_lease_check(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    seen: list[str] = []

    def verify(root_path: str, raw: bytes | None) -> None:
        seen.append(root_path)
        raise PermissionError("lease lost")

    env.fsops.enforce_lease(verify)
    with pytest.raises(PermissionError):
        env.fsops.write_mirror(str(env.library), mirror_name(new_ulid(), new_ulid()), b"{}\n")
    assert seen == [str(env.library)]


def test_ordinary_paths_still_cannot_reach_the_manifest_folder(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    with pytest.raises(ReservedPathError):
        env.fsops.write_atomic(
            str(env.library / ".shelfhold" / "manifest" / "x.jsonl"), b"{}", parents=True
        )
