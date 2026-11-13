"""M2 L20: the import folder as an admin uses it (EF-SRC-012).

The setting, the command `shelfhold import scan`, the scheduled job, the Work/Edition/Unit made
through the road of the sources (the new-Edition door included). A real instance: database,
keyring, roots, lease; the files and the stores are the production ones.
"""

import os
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from shelfhold.config import Settings
from shelfhold.import_jobs import IMPORT_KIND, import_handler, register_import_job
from shelfhold.infrastructure.schema import acquisition, catalog, storage
from shelfhold.library.adapters.import_scan import QUARANTINE
from shelfhold.library.adapters.storage import sha256_file
from shelfhold.main import LOGGED_SETTINGS
from shelfhold.sdk.testing.corpus.trapped_epubs.build import Member
from tests.auth_env import run_cli
from tests.import_env import (
    AES,
    CLEAN_EPUB,
    PDF,
    SCRIPT_EPUB,
    encryption_xml,
    epub_with,
)
from tests.l41_env import context
from tests.l50_env import Seeded, seeded
from tests.library_env import PNG, cbz_bytes

CBZ = cbz_bytes()
OLD = 3_600


def drop(folder: Path, name: str, data: bytes) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - OLD
    os.utime(path, (stamp, stamp))
    return path


def counts(env: Seeded) -> dict[str, int]:
    tables = {"work": catalog.work, "unit": catalog.unit, "variant": catalog.unit_variant,
              "content": storage.content, "item": acquisition.source_item}  # fmt: skip
    with env.inst.engine.connect() as connection:
        return {
            name: connection.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
            for name, t in tables.items()
        }


def folder_for(tmp_path: Path) -> Path:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return folder


@pytest.mark.req("EF-SRC-012")
def test_the_import_dir_may_not_overlap_the_volumes(settings: Settings, tmp_path: Path) -> None:
    ok = settings.model_copy(update={"import_dir": tmp_path / "incoming"})
    assert Settings.model_validate(ok.model_dump()).import_dir == tmp_path / "incoming"
    for bad in (
        settings.data_dir, settings.store_dir, settings.library_dir,
        settings.data_dir / "incoming", settings.library_dir / "incoming",
        settings.store_dir.parent, Path("/"),
    ):  # fmt: skip
        with pytest.raises(ValidationError, match="import_dir"):
            Settings.model_validate({**settings.model_dump(), "import_dir": bad})


@pytest.mark.req("EF-SRC-012")
def test_another_name_for_a_root_is_refused_by_device_and_inode(
    settings: Settings, tmp_path: Path
) -> None:
    """A link (the same device and inode as a bind mount would show) under another path."""
    library = tmp_path / "lib"
    library.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(library)
    with pytest.raises(ValidationError, match="import_dir"):
        Settings.model_validate(
            {**settings.model_dump(), "library_dir": library, "import_dir": alias}
        )


@pytest.mark.req("EF-SRC-012", "ENF-LEG-009")
def test_the_import_dir_is_a_logged_and_classed_setting() -> None:
    from shelfhold.ops.diagnostic import SETTING_CLASSES

    assert LOGGED_SETTINGS["import_dir"] == "import_dir"
    assert SETTING_CLASSES["import_dir"] == "presence"  # the path itself never enters a bundle


@pytest.mark.req("EF-SRC-012")
def test_the_scan_is_off_without_the_setting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("SHELFHOLD_IMPORT_DIR", raising=False)
    with seeded(settings, tmp_path / "inst") as env:
        assert run_cli(monkeypatch, settings, "import", "scan") != 0
        assert "Aucun dossier d'import" in capsys.readouterr().err
        scheduled: list[str] = []

        class Pools:
            scheduler = type("S", (), {"add": lambda self, kind, *a, **k: scheduled.append(kind)})()

            def register(self, kind: str, **_: Any) -> None:
                scheduled.append(kind)

        register_import_job(Pools(), env.inst.comp)  # type: ignore[arg-type]
        assert scheduled == []
        assert counts(env)["unit"] == 0


@pytest.mark.req("EF-SRC-012", "EF-BIB-055", "EF-BIB-051")
def test_a_file_dropped_in_the_folder_is_stored_verified_and_removed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """End to end: files are dropped, `import scan` stores and verifies them, then removes them;
    the protected one stays with its message."""
    folder = folder_for(tmp_path)
    monkeypatch.setenv("SHELFHOLD_IMPORT_DIR", str(folder))
    protected = epub_with(Member("META-INF/encryption.xml", encryption_xml(AES, "OEBPS/ch1.xhtml")))
    with seeded(settings, tmp_path / "inst") as env:
        drop(folder, "one.cbz", CBZ)
        drop(folder, "sub/two.epub", SCRIPT_EPUB)
        drop(folder, "three.pdf", PDF)
        locked = drop(folder, "locked.epub", protected)
        capsys.readouterr()

        assert run_cli(monkeypatch, settings, "import", "scan", "--dry-run") == 0
        out = capsys.readouterr().out
        assert (
            out.count("Prêt à être rangé") == 4
        )  # a dry run reads no file: it lists what is ready
        assert counts(env)["unit"] == 0 and len(list(folder.rglob("*.*"))) == 4  # nothing moved

        assert run_cli(monkeypatch, settings, "import", "scan") == 0
        out = capsys.readouterr().out
        assert "3 rangés, 1 refusé" in out
        assert "Fichier protégé" in out
        assert [p.name for p in folder.rglob("*") if p.is_file()] == ["locked.epub"]
        assert locked.read_bytes() == protected  # untouched

        n = counts(env)
        assert (n["work"], n["unit"], n["variant"], n["content"]) == (3, 3, 3, 3)
        with env.inst.engine.connect() as connection:
            editions = connection.execute(sa.select(catalog.edition.c.shelf_id)).all()
            rows = list(connection.execute(sa.select(storage.content)))
        # the new-Edition door filed them on the shelf of the instance (an 18+ shelf takes them)
        assert all(e.shelf_id == env.shelf_id for e in editions)
        assert sorted(r.format for r in rows) == ["cbz", "epub", "pdf"]
        library = [p for p in (env.inst.library).rglob("*") if p.is_file()]
        stored = {sha256_file(p) for p in library}
        for row in rows:  # what the library holds is the file that was dropped (or its derivative)
            assert row.original_sha256 in {sha256_file_bytes(b) for b in (CBZ, SCRIPT_EPUB, PDF)}
        assert any(sha == sha256_file_bytes(CBZ) for sha in stored)
        assert any(Path(env.inst.settings.store_dir).rglob("*.epub"))  # the original is kept

        assert run_cli(monkeypatch, settings, "import", "scan") == 0  # nothing new, nothing again
        assert counts(env) == n


def sha256_file_bytes(data: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.mark.req("EF-SRC-012")
def test_the_scheduled_job_takes_in_the_folder_and_asks_for_the_export(
    settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.composition import Composition
    from shelfhold.infrastructure.clock import SystemClock

    folder = folder_for(tmp_path)
    with seeded(settings, tmp_path / "inst") as env:
        comp = Composition(
            settings.model_copy(update={"import_dir": folder}),
            env.inst.engine,
            clock=SystemClock(),
            network=env.inst.network,
        )
        drop(folder, "one.cbz", CBZ)
        exported: list[int] = []
        import_handler(comp, lambda: exported.append(1))(context(IMPORT_KIND))
        assert exported == [1] and counts(env)["unit"] == 1
        assert [p.name for p in folder.iterdir() if p.name != QUARANTINE] == []
        scheduled: list[str] = []

        class Pools:
            scheduler = type("S", (), {"add": lambda self, kind, *a, **k: scheduled.append(kind)})()

            def register(self, kind: str, **kw: Any) -> None:
                assert kw["pool"] == "io"

        register_import_job(Pools(), comp)  # type: ignore[arg-type]
        assert scheduled == [IMPORT_KIND]


@pytest.mark.req("EF-SRC-012", "EF-BIB-051")
def test_a_refused_epub_leaves_no_empty_unit_behind(settings: Settings, tmp_path: Path) -> None:
    """The neutralizer refuses the book after the Unit was made: the Unit, its Variant, binding
    and Work are taken back, and the file stays in the folder."""
    from shelfhold.composition import Composition
    from shelfhold.infrastructure.clock import SystemClock

    folder = folder_for(tmp_path)
    with seeded(settings, tmp_path / "inst") as env:
        comp = Composition(
            settings.model_copy(update={"import_dir": folder}),
            env.inst.engine,
            clock=SystemClock(),
            network=env.inst.network,
        )
        before = counts(env)
        trapped = epub_with(Member("OEBPS/run.js", b"alert(1)"))
        path = drop(folder, "trap.epub", trapped)
        from shelfhold.ops.import_folder import ImportOps

        report = ImportOps(comp).scan()
        assert [o["code"] for o in report["outcomes"]] == ["import.archive_refused"]
        assert path.read_bytes() == trapped
        assert counts(env) == before  # not one row of the Unit remains


@pytest.mark.req("EF-SRC-012", "EF-BIB-041")
def test_a_declared_purchase_is_published_and_flagged(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    folder = folder_for(tmp_path)
    monkeypatch.setenv("SHELFHOLD_IMPORT_DIR", str(folder))
    with seeded(settings, tmp_path / "inst") as env:
        drop(folder, "achats/bought.epub", CLEAN_EPUB)
        assert run_cli(monkeypatch, settings, "import", "scan", "--purchase") == 0
        with env.inst.engine.connect() as connection:
            row = connection.execute(sa.select(storage.content)).one()
            trust = connection.execute(sa.select(acquisition.source_binding.c.content_trust)).all()
        assert (row.watermarked, row.watermark_origin, row.derived_hash) == (True, "user", None)
        assert ("publisher",) in [tuple(t) for t in trust]


SVG_PAGE = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
    b'<rect width="8" height="8" fill="#336699"/></svg>'
)
HOSTILE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><script/></svg>'


@pytest.mark.req("EF-BIB-051")
def test_import_folder_rasterizes_svg_pages_into_a_declared_derivative(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """End to end (M2 L10): an untrusted CBZ with an SVG page arrives by the import folder; the
    library holds the PNG rewrite declared as the derivative, /store keeps the original; a hostile
    SVG is refused closed and the file stays where it was, with no Unit."""
    folder = folder_for(tmp_path)
    monkeypatch.setenv("SHELFHOLD_IMPORT_DIR", str(folder))
    good = cbz_bytes((("001.png", PNG), ("002.svg", SVG_PAGE)))
    hostile = cbz_bytes((("001.png", PNG), ("002.svg", HOSTILE_SVG)))
    with seeded(settings, tmp_path / "inst") as env:
        drop(folder, "good.cbz", good)
        bad = drop(folder, "hostile.cbz", hostile)
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "import", "scan") == 0
        assert [p.name for p in folder.rglob("*") if p.is_file()] == ["hostile.cbz"]
        assert bad.read_bytes() == hostile  # refused closed: left in place, untouched
        assert counts(env)["unit"] == 1 and counts(env)["content"] == 1  # no empty Unit
        with env.inst.engine.connect() as connection:
            row = connection.execute(sa.select(storage.content)).one()
        assert row.original_sha256 == sha256_file_bytes(good)
        assert row.derived_hash is not None and row.derived_by == "svg-rasterizer@v1"
        served = [p for p in env.inst.library.rglob("*.cbz")]
        assert [sha256_file(p) for p in served] == [row.derived_hash]
        kept = [p for p in Path(env.inst.settings.store_dir).rglob("*.cbz")]
        assert [sha256_file(p) for p in kept] == [row.original_sha256]
        with zipfile.ZipFile(served[0]) as archive:
            names = archive.namelist()
        assert "002.png" in names and "002.svg" not in names


def _scan_with_declaration_busy_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
    *,
    stay_busy: bool,
) -> tuple[Path, Any, Any, Seeded]:
    from shelfhold.library.adapters.storage import KeepCopier
    from shelfhold.library.domain.failures import SafeguardFailedError

    folder = folder_for(tmp_path)
    monkeypatch.setenv("SHELFHOLD_IMPORT_DIR", str(folder))
    good = cbz_bytes((("001.png", PNG), ("002.svg", SVG_PAGE)))
    real = KeepCopier.declare_derivative

    def busy(*_a: Any, **_k: Any) -> Any:
        raise SafeguardFailedError("work_busy", "held")

    cm = seeded(settings, tmp_path / "inst")
    env = cm.__enter__()
    source = drop(folder, "good.cbz", good)
    monkeypatch.setattr(KeepCopier, "declare_derivative", busy)
    run_cli(monkeypatch, settings, "import", "scan")
    assert source.exists()  # the first pass failed: the source is kept
    if not stay_busy:
        monkeypatch.setattr(KeepCopier, "declare_derivative", real)  # the Work is free again
    run_cli(monkeypatch, settings, "import", "scan")
    capsys.readouterr()
    with env.inst.engine.connect() as connection:
        row = connection.execute(sa.select(storage.content)).one()
    return source, row, cm, env


@pytest.mark.req("EF-BIB-051")
def test_replay_after_a_busy_work_finishes_the_derivative_before_removing_the_source(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    source, row, cm, env = _scan_with_declaration_busy_once(
        monkeypatch, capsys, settings, tmp_path, stay_busy=False
    )
    try:
        assert row.derived_hash is not None and row.derived_by == "svg-rasterizer@v1"
        assert not source.exists()  # removed only once the derivative exists
        kept = list(Path(env.inst.settings.store_dir).rglob("*.cbz"))
        assert [sha256_file(p) for p in kept] == [row.original_sha256]
        with zipfile.ZipFile(next(env.inst.library.rglob("*.cbz"))) as archive:
            assert "002.png" in archive.namelist()
    finally:
        cm.__exit__(None, None, None)


@pytest.mark.req("EF-BIB-051")
def test_the_source_with_svg_pages_is_never_removed_while_its_derivative_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    source, row, cm, _env = _scan_with_declaration_busy_once(
        monkeypatch, capsys, settings, tmp_path, stay_busy=True
    )
    try:
        assert row.derived_hash is None
        assert source.exists()  # still there, to be tried again
    finally:
        cm.__exit__(None, None, None)
