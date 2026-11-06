"""L56: `shelfhold meta rebuild|check` regenerates every projection of the database, the provenance
mirror included (EF-EXP-063, ENF-OPS-020), through the instance (its lock, the write lease).

The mirror is untrusted input: the rebuild writes what the database holds, keeps what it cannot
explain as `unresolved`, and never creates an identity, a Unit or a row from what a line says.
"""

import json
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.instance import InstanceLock
from shelfhold.infrastructure.schema import catalog, export, identity
from tests.l50_env import Seeded, Series
from tests.l51_env import live
from tests.l54_env import Bench54, bench54
from tests.mirror_env import entry, raw

pytestmark = pytest.mark.req("EF-EXP-063", "ENF-OPS-020")

TABLES = (
    catalog.work, catalog.edition, catalog.unit, catalog.unit_variant, export.export_item,
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


def test_meta_rebuild_takes_no_age_or_lineage_from_a_line_of_the_disk(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        path = _exported(b)
        good = _lines(path)
        owned = good[1]
        assert "age_classified" not in owned and "derived_hash" not in owned
        # the very file the database knows, with an age, a derived hash and a note added by hand
        forged = {
            **owned,
            "age_classified": {"min_age": 0, "x": False},
            "derived_hash": "sha256:" + "0" * 64,
            "derived_by": "x@1",
            "note": "kept: unknown fields travel",
        }
        path.write_bytes(raw(good[0], forged))
        assert b.cli("meta", "rebuild") == 0
        line = _lines(path)[1]
        assert "age_classified" not in line and "derived_hash" not in line
        assert "derived_by" not in line
        assert line["note"] == "kept: unknown fields travel"
        assert line["state"] == "exported" and line["coverage"] == owned["coverage"]


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


def test_projections_lost_in_a_crash_are_written_again_when_the_server_starts(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
) -> None:
    lost: list[Path] = []

    def crash(env: Seeded, _series: Series) -> None:
        # the store committed and died before its files: nothing of meta/ is left
        for path in (settings.data_dir / "meta").rglob("*.json"):
            lost.append(path)
            path.unlink()

    with live(monkeypatch, settings, tmp_path, chapters=("1",), seed=crash):
        assert len(lost) == 2  # one unit.json, one work.json
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not all(p.exists() for p in lost):
            time.sleep(0.1)
        assert all(p.exists() for p in lost)
        assert json.loads(lost[0].read_bytes())["schema_version"] in {"1.0", "1.1"}
