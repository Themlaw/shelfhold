"""ENF-OPS-002: a published migration is never edited, a patch release carries no new one."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.tools_loader import REPO, load_tool

guard = load_tool("check_patch_migrations")


def _copy(tmp_path: Path) -> Path:
    for rel in (guard.VERSIONS, "tools"):
        shutil.copytree(REPO / rel, tmp_path / rel, ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


@pytest.mark.req("ENF-OPS-002")
def test_every_migration_of_main_is_frozen() -> None:
    # The pull-request CI runs the same check without --tag: a PR that adds a migration freezes it
    # itself (`--freeze <minor>`: 1.1 for the M4 line, 1.2 for the M5 migrations, which leave with
    # `v1.2.0`), so a release never finds one unfrozen.
    assert guard.check(REPO) == []


@pytest.mark.req("ENF-OPS-002")
def test_patch_release_refuses_modified_migration(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    first = next((root / guard.VERSIONS).glob("0001_*.py"))
    first.write_text(first.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    for tag in ("v0.2.0", "v0.1.1"):  # a minor release refuses it too
        assert any("modified" in p for p in guard.check(root, tag)), tag


@pytest.mark.req("ENF-OPS-002")
def test_patch_release_refuses_added_migration(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    (root / guard.VERSIONS / "9999_new.py").write_text("revision = '9999'\n", encoding="utf-8")
    assert any("not in" in p for p in guard.check(root, "v0.2.0"))
    guard.freeze(root, "0.3")  # reviewed and frozen as shipped from 0.3
    assert guard.check(root, "v0.3.0") == []
    assert any("9999_new.py" in p for p in guard.check(root, "v0.2.1"))
    assert not any("9999_new.py" in p for p in guard.check(root, "v0.3.1"))


@pytest.mark.req("ENF-OPS-002")
def test_a_published_migration_cannot_be_removed(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    next((root / guard.VERSIONS).glob("0012_*.py")).unlink()
    assert any("removed" in p for p in guard.check(root))


@pytest.mark.req("ENF-OPS-002")
def test_the_oldest_migration_is_the_one_of_the_first_release() -> None:
    # byte for byte what v0.1.0 shipped: line endings are normalised, nothing else
    entry = guard.load_frozen(REPO)["0001_initial.py"]
    assert entry["since"] == "0.1"
    assert entry["sha256"] == "f0630dd5a5c56d4823365640e660846d461885861f7e27fd9fb7ab8dd047748c"


@pytest.mark.req("ENF-OPS-002")
def test_release_candidate_tag_is_accepted_and_keeps_the_rules(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    for plain, candidate in (("v0.2.0", "v0.2.0-rc.1"), ("v0.2.1", "v0.2.1-rc.2")):
        expected = [p.replace(plain, candidate) for p in guard.check(root, plain)]
        assert guard.check(root, candidate) == expected
    for bad in ("v0.2.0-rc1", "v0.2.0-rc.0", "v0.2.0-beta.1", "0.2.0-rc.1"):
        assert any("not a vX.Y.Z" in p for p in guard.check(root, bad)), bad
