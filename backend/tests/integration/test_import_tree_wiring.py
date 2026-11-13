"""M2 L21: `shelfhold import tree <path>` as an admin uses it (EF-INT-009).

A real instance (database, keyring, roots, stores) and the command in process: the Units are made
from the files of a Suwayomi tree, attached to the binding of the series, and the tree is left as
it was.
"""

import os
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import acquisition, catalog, storage
from tests.auth_env import run_cli
from tests.l50_env import seeded
from tests.library_env import PNG, cbz_bytes

CBZ = cbz_bytes()
OLD = 3_600


def drop(root: Path, rel: str, data: bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - OLD
    os.utime(path, (stamp, stamp))
    return path


def listing(root: Path) -> list[tuple[str, int]]:
    return [(str(p.relative_to(root)), p.lstat().st_mtime_ns) for p in sorted(root.rglob("*"))]


@pytest.mark.req("EF-INT-009")
def test_import_tree_command_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    tree = tmp_path / "downloads"
    tree.mkdir()
    with seeded(settings, tmp_path / "inst") as env:
        env.new_series("Dr Stone")
        with env.inst.engine.begin() as connection:  # the source of the binding: `Source (EN)`
            connection.execute(
                sa.insert(acquisition.sub_source).values(
                    plugin_id="suwayomi",
                    source_key="suwayomi:1",
                    name="Source",
                    lang="en",
                    content_warning="safe",
                )
            )
        drop(tree, "mangas/Source (EN)/Dr Stone/Chapter 1.cbz", CBZ)
        drop(tree, "mangas/Source (EN)/Dr Stone/Chapter 2/002.png", PNG + b"2")
        drop(tree, "mangas/Source (EN)/Dr Stone/Chapter 2/001.png", PNG + b"1")
        drop(tree, "mangas/Source (EN)/Unfollowed/Chapter 1.cbz", CBZ)
        before = listing(tree)
        capsys.readouterr()

        assert run_cli(monkeypatch, settings, "import", "tree", str(tree), "--dry-run") == 0
        assert capsys.readouterr().out.count("Prêt à être rangé") == 3
        with env.inst.engine.connect() as connection:
            units = sa.select(sa.func.count()).select_from(catalog.unit)
            assert connection.execute(units).scalar_one() == 0

        assert run_cli(monkeypatch, settings, "import", "tree", str(tree)) == 0
        out = capsys.readouterr().out
        assert "2 rangés, 1 ignoré, 0 refusé" in out
        assert "Aucune liaison Suwayomi" in out
        with env.inst.engine.connect() as connection:
            assert connection.execute(sa.select(sa.func.count()).select_from(storage.content)
                                      ).scalar_one() == 2  # fmt: skip
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(catalog.work)
                ).scalar_one()
                == 1
            )  # no Work was made from a folder name
        assert listing(tree) == before  # the tree is as it was

        assert run_cli(monkeypatch, settings, "import", "tree", str(tree)) == 0
        assert "0 rangé, 3 ignorés" in capsys.readouterr().out  # twice: nothing new


@pytest.mark.req("EF-INT-009")
def test_import_tree_refuses_a_path_that_is_not_a_real_folder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with seeded(settings, tmp_path / "inst") as env:
        for bad in ("relative/path", str(tmp_path / "link"), str(tmp_path / "missing")):
            assert run_cli(monkeypatch, settings, "import", "tree", bad) != 0
            assert "pas lisible" in capsys.readouterr().err
        assert run_cli(monkeypatch, settings, "import", "tree", str(env.inst.library)) != 0
        assert "pas lisible" in capsys.readouterr().err  # a root of ours is not a tree to import
