"""L45, EF-EXP-006: the files of /library follow `SHELFHOLD_UMASK`; the secrets stay 0600.

A real export through the production wiring (`open_pipeline`, `open_fsops`) over real files, with
the process umask set the way `shelfhold` sets it at start (077): the library files must not
inherit it, and the keyring must not depend on it.
"""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import ConfigError, load_settings
from shelfhold.export.application.roots import load_roots, open_fsops
from shelfhold.export.application.wiring import open_pipeline
from shelfhold.export.domain.path_map import PathMap
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from shelfhold.infrastructure.secrets import diagnostics
from tests.export_env import ExportEnv, export_env
from tests.fakes.kavita.plugin_parse import simulate_parse

PROCESS_UMASK = 0o077  # what the CLI entry point imposes on the process


@contextmanager
def process_umask(value: int) -> Iterator[None]:
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


def _export(env: ExportEnv, environ: dict[str, str]) -> list[Path]:
    """One chapter exported by the production pipeline, configured from the environment."""
    data_dir = Path(env.engine.url.database or "").parent  # where the database lives
    (data_dir / "tmp").mkdir(exist_ok=True)
    settings = load_settings({"SHELFHOLD_DATA_DIR": str(data_dir), **environ}).values
    pipeline = open_pipeline(settings)
    pipeline.bind_parse(simulate_parse, verified=True)
    series = env.new_series()
    env.add_chapter(series, "2")
    pipeline.run(env.destination_id, job_id=new_ulid())
    return env.files()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.req("EF-EXP-006")
@pytest.mark.parametrize(
    ("environ", "file_mode", "dir_mode"),
    [
        ({"SHELFHOLD_UMASK": "0002"}, 0o664, 0o775),
        ({"SHELFHOLD_UMASK": "0027"}, 0o640, 0o750),
        ({}, 0o644, 0o755),  # the spec fixes no default: 0022 (decisions M1a, L45)
    ],
)
def test_library_files_follow_shelfhold_umask(
    environ: dict[str, str], file_mode: int, dir_mode: int
) -> None:
    with export_env() as env, process_umask(PROCESS_UMASK):
        files = _export(env, environ)
        assert [p.name for p in files] == ["Dr Stone - Ch.2.cbz"]
        assert _mode(files[0]) == file_mode
        assert _mode(files[0].parent) == dir_mode


@pytest.mark.req("EF-EXP-006")
@pytest.mark.parametrize("umask", [0o000, 0o077])
def test_secrets_stay_0600_whatever_the_umask(umask: int, tmp_path: Path) -> None:
    settings = load_settings(
        {
            "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
            "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
            "SHELFHOLD_UMASK": "0022",
        }
    ).values
    with process_umask(umask):
        diagnostics.boot_check(settings, create=True, announce=False)
    keyrings = [
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and "key" in p.name and p.parent.name == "data"
    ]
    assert keyrings, "boot_check must have created the keyring"
    assert {_mode(p) for p in keyrings} == {0o600}


@pytest.mark.req("EF-EXP-006")
def test_umask_setting_is_octal_and_validated() -> None:
    assert load_settings({"SHELFHOLD_UMASK": "0027"}).values.umask == 0o027
    assert load_settings({}).values.umask == 0o022
    for bad in ("0999", "-1", "1000", "rw"):
        with pytest.raises(Exception, match="umask"):
            load_settings({"SHELFHOLD_UMASK": bad})


@pytest.mark.req("EF-EXP-006")
@pytest.mark.parametrize("umask", ["0000", "0020", "0700", "0200", "0001"])
def test_umask_that_breaks_files_or_opens_them_to_everyone_is_refused(umask: str) -> None:
    with pytest.raises(ConfigError, match="umask"):
        load_settings({"SHELFHOLD_UMASK": umask})


@pytest.mark.req("EF-EXP-006")
@pytest.mark.parametrize("umask", ["0002", "0022", "0027", "0077"])
def test_usable_umasks_are_accepted(umask: str) -> None:
    assert load_settings({"SHELFHOLD_UMASK": umask}).values.umask == int(umask, 8)


@pytest.mark.req("EF-EXP-006")
def test_host_area_stays_owner_only_whatever_the_library_umask() -> None:
    """The lease, the staging, the recycle bin, the received archives, the mirror and the
    sentinel are Shelfhold's own: 0700 / 0600 even under SHELFHOLD_UMASK=0002. The production
    `open_fsops` does the writing, the export included."""
    with export_env() as env, process_umask(PROCESS_UMASK):
        files = _export(env, {"SHELFHOLD_UMASK": "0002"})
        with env.engine.connect() as connection:
            paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
            loaded = load_roots(connection, PathMap.same_path(paths))
        fsops = open_fsops(loaded, umask=0o002)
        library = str(env.lib.library)
        fsops.update_lease(library, lambda _current: b"lease")
        fsops.write_atomic(f"{library}/.shelfhold/received/x.cbz", b"x", parents=True)
        fsops.recycle(str(files[0]))
        area = env.lib.library / ".shelfhold"
        seen = [area, *area.rglob("*"), env.lib.library / ".shelfhold-mount-id"]
        names = {p.name for p in seen}
        assert {"lease", "received", "recycle", "staging"} <= names, names
        for path in seen:
            if path.is_file() and "recycle" in path.parts:
                continue  # a library file put in the bin keeps the mode it had
            wanted = 0o700 if path.is_dir() else 0o600
            assert _mode(path) == wanted, (str(path), oct(_mode(path)))
