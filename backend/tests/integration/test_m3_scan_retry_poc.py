"""#263, relecture adverse (effacement et intégrité): the holes found in the retry of the lost
Kavita scans and in the suggestion at the arrival. They were red on purpose (`xfail` strict); the
fixes turned them green and the marks are gone."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.pending_scans import (
    ScanRefusedError,
    load_pending_scans,
    remember_scans,
)
from shelfhold.export.targets.kavita.errors import ProtocolError
from shelfhold.infrastructure.schema import catalog
from shelfhold.ops.library import LibraryOps, ScanUnavailableError
from tests.auth_env import run_cli
from tests.export_env import export_env
from tests.l50_env import seeded
from tests.library_env import PNG, cbz_bytes
from tests.purge_env import ADMIN


@pytest.mark.req("EF-BIB-021")
def test_poc_a_scan_written_down_during_the_retry_is_not_lost() -> None:
    with export_env() as env:
        remember_scans(env.engine, ["/data/Kavita/Dr Stone"])

        class Client:
            async def scan_folder(self, path: str) -> None:
                # meanwhile, a withdrawal (or a detachment) loses its own scan and writes it down
                remember_scans(env.engine, ["/data/Kavita/Vinland"])

        runtime = SimpleNamespace(engine=env.engine, kavita_client=lambda _target: Client())
        LibraryOps(runtime).retry_pending_scans()  # type: ignore[arg-type]
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == ["/data/Kavita/Vinland"]


@pytest.mark.req("EF-BIB-021")
def test_poc_one_folder_that_cannot_be_scanned_does_not_hold_the_others(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seeded(settings, tmp_path) as env:
        stone = env.add_chapter(env.new_series("Dr Stone"), "1")
        akira = env.add_chapter(env.new_series("Akira"), "1")
        env.export()
        ops = LibraryOps(env.inst.comp)

        def down(_self: object, _paths: list[str]) -> None:
            raise ScanUnavailableError("Kavita is down")

        with monkeypatch.context() as patch:
            patch.setattr(LibraryOps, "_scan_everywhere", down)
            preview = ops.delete_preview(level="remove_destinations", unit_ids=[stone, akira])
            ops.delete_apply(
                level="remove_destinations", unit_ids=[stone, akira],
                if_match=preview["version"], typed={}, principal=ADMIN,
            )  # fmt: skip

        def one_refused(_self: object, paths: list[str]) -> None:
            if any("Akira" in p for p in paths):  # Kavita answers 400 for this one path
                raise ScanRefusedError(str(ProtocolError("scan-folder answered 400", status=400)))

        monkeypatch.setattr(LibraryOps, "_scan_everywhere", one_refused)
        ops.retry_pending_scans()
        assert not (env.inst.library / "Kavita" / "Dr Stone").exists()


@pytest.mark.req("EF-BIB-021")
def test_poc_scan_pending_is_not_said_when_kavita_answered(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with seeded(settings, tmp_path) as env:
        unit = env.add_chapter(env.new_series("Dr Stone"), "1")
        env.export()
        ops = LibraryOps(env.inst.comp)
        folder = env.inst.library / "Kavita" / "Dr Stone"
        preview = ops.delete_preview(level="remove_destinations", unit_ids=[unit])

        def answered(_self: object, _paths: list[str]) -> None:
            (folder / "arrived.txt").write_text("a file put there meanwhile")

        monkeypatch.setattr(LibraryOps, "_scan_everywhere", answered)
        done = ops.delete_apply(
            level="remove_destinations", unit_ids=[unit], if_match=preview["version"],
            typed={}, principal=ADMIN,
        )  # fmt: skip
        assert (folder / "arrived.txt").exists()  # never removed: rmdir refuses (no data lost)
        assert done["scan_pending"] is False


def _drop(folder: Path, name: str, data: bytes) -> None:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - 3_600
    os.utime(path, (stamp, stamp))


@pytest.mark.req("EF-BIB-004")
def test_poc_a_failed_suggestion_does_not_fail_an_import_that_stored(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    import shelfhold.ops.dedup as dedup

    folder = tmp_path / "incoming"
    folder.mkdir()
    monkeypatch.setenv("SHELFHOLD_IMPORT_DIR", str(folder))

    def locked(*_a: object, **_k: object) -> list[str]:
        raise sa.exc.OperationalError("INSERT", {}, Exception("database is locked"))

    monkeypatch.setattr(dedup, "suggest_duplicates", locked)
    with seeded(settings, tmp_path / "inst") as env:
        _drop(folder, "La Voie.cbz", cbz_bytes((("001.png", PNG),)))
        code = run_cli(monkeypatch, settings, "import", "scan")
        with env.inst.engine.connect() as connection:
            works = connection.execute(sa.select(sa.func.count()).select_from(catalog.work))
            assert works.scalar_one() == 1  # the file is stored
        assert code == 0
