"""L56: `shelfhold meta rebuild|check` regenerates every projection of the database, the provenance
mirror included (EF-EXP-063, ENF-OPS-020), through the instance (its lock, the write lease).

The mirror is untrusted input: the rebuild writes what the database holds, keeps what it cannot
explain as `unresolved`, and never creates an identity, a Unit or a row from what a line says.
"""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.instance import InstanceLock
from shelfhold.infrastructure.schema import acquisition, catalog, export, identity
from tests.l54_env import Bench54, bench54
from tests.mirror_env import entry, raw

pytestmark = pytest.mark.req("EF-EXP-063", "ENF-OPS-020")

TABLES = (
    catalog.work, catalog.edition, catalog.unit, acquisition.variant, export.export_item,
    export.export_record, export.unit_coverage, export.export_series, identity.user,
)  # fmt: skip


def _counts(b: Bench54) -> dict[str, int]:
    with b.engine.connect() as connection:
        return {
            t.name: connection.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
            for t in TABLES
        }


def _exported(b: Bench54) -> Path:
    """One chapter exported by the production pipeline: its mirror file, on disk."""
    series = b.env.new_series()
    b.env.add_chapter(series, "1")
    b.env.export()
    (path,) = (b.env.inst.library / ".shelfhold" / "manifest").rglob("*.jsonl")
    return path


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_meta_rebuild_rewrites_provenance_mirror_keeps_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        path = _exported(b)
        good = path.read_bytes()
        units = sorted((settings.data_dir / "meta" / "units").rglob("*.json"))
        works = sorted((settings.data_dir / "meta" / "works").rglob("*.json"))
        assert len(units) == 1 and len(works) == 1
        # a crash lost the three projections
        path.unlink()
        units[0].unlink()
        works[0].unlink()
        capsys.readouterr()
        assert b.cli("meta", "check") == 1
        drift = capsys.readouterr().out
        assert path.name in drift and units[0].stem in drift and works[0].stem in drift
        assert not path.exists()  # `check` writes nothing
        assert b.cli("meta", "rebuild") == 0
        assert path.read_bytes() == good and units[0].exists() and works[0].exists()
        assert b.cli("meta", "check") == 0
        # the write lease was taken for the rewrite and given back
        assert not (b.env.inst.library / ".shelfhold" / "lease").exists()
        # a line the database cannot explain (a series born after the last backup) stays
        stranger = entry("Dr Stone/born-after-backup.cbz", state="exported")
        path.write_bytes(good + raw(stranger))
        before = _counts(b)
        for _ in range(2):
            assert b.cli("meta", "rebuild") == 0
            kept = {line.get("rel_path"): line for line in _lines(path)[1:]}
            assert kept["Dr Stone/born-after-backup.cbz"]["state"] == "unresolved"
            assert kept["Dr Stone/born-after-backup.cbz"]["export_item"] == stranger["export_item"]
            assert len(kept) == 2
        # untrusted input: nothing in the database was created from the ids the line carries
        assert _counts(b) == before
        with b.engine.connect() as connection:
            known = connection.execute(sa.select(catalog.unit.c.id)).scalars().all()
        assert stranger["coverage"][0]["unit"] not in known  # type: ignore[index]


def test_meta_rebuild_never_follows_a_forged_path_or_a_foreign_header(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        path = _exported(b)
        outside = tmp_path / "outside.cbz"
        forged = entry("../../outside.cbz", state="exported")
        path.write_bytes(path.read_bytes() + raw(forged))
        before = _counts(b)
        assert b.cli("meta", "rebuild") == 0
        assert not outside.exists() and _counts(b) == before
        assert [p.name for p in path.parent.rglob("*") if p.is_file()] == [path.name]


def test_meta_rebuild_goes_through_the_instance_lock(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        path = _exported(b)
        path.unlink()
        capsys.readouterr()
        server = InstanceLock(settings.data_dir)  # a running instance holds the lock
        server.acquire()
        try:
            assert b.cli("meta", "rebuild") != 0
        finally:
            server.release()
        assert not path.exists()  # nothing was written behind the instance's back
        assert b.cli("meta", "rebuild") == 0 and path.exists()
