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

from shelfhold.config import load_settings
from shelfhold.export.application.wiring import open_pipeline
from shelfhold.infrastructure.ids import new_ulid
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
        ({}, 0o644, 0o755),  # the default of the spec
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
            "SHELFHOLD_UMASK": "0000",
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
