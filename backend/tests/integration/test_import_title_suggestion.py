"""EF-BIB-004 at the import (01): two identical titles without a common identifier give a
suggestion, never a merge, when the files arrive (import folder), not only on `dedup scan`."""

import os
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import catalog
from tests.auth_env import run_cli
from tests.l50_env import seeded
from tests.library_env import JPEG, PNG, cbz_bytes


def _drop(folder: Path, name: str, data: bytes) -> None:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - 3_600
    os.utime(path, (stamp, stamp))


@pytest.mark.req("EF-BIB-004")
def test_an_import_of_two_identical_titles_suggests_and_never_merges(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    folder = tmp_path / "incoming"
    folder.mkdir()
    monkeypatch.setenv("SHELFHOLD_IMPORT_DIR", str(folder))
    with seeded(settings, tmp_path / "inst") as env:
        _drop(folder, "La Voie.cbz", cbz_bytes())
        _drop(folder, "other/La Voie.cbz", cbz_bytes((("001.png", PNG), ("002.png", PNG + b"x"))))
        _drop(folder, "Le Mur.cbz", cbz_bytes((("001.jpg", JPEG),)))
        assert run_cli(monkeypatch, settings, "import", "scan") == 0
        with env.inst.engine.connect() as connection:
            works = connection.execute(sa.select(sa.func.count()).select_from(catalog.work))
            assert works.scalar_one() == 3  # never a merge
            rows = connection.execute(
                sa.select(catalog.merge_suggestion.c.reason, catalog.merge_suggestion.c.state)
            ).all()
        assert [(r.reason, r.state) for r in rows] == [("same_title", "open")]  # one pair only
