"""No fixture leaves a folder in the system temporary folder (a machine once held 164)."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.catalog_env import _template


def test_catalog_template_lives_under_the_pytest_base_temporary_folder(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    template = _template()
    assert Path(tmp_path_factory.getbasetemp()) in template.parents, template


def test_session_fails_when_a_fixture_leaks_a_template_folder(tmp_path: Path) -> None:
    (tmp_path / "test_leaks.py").write_text(
        "import tempfile\n\n"
        "def test_leaks():\n"
        '    tempfile.mkdtemp(prefix="shelfhold-catalog-template-")\n',
        encoding="utf-8",
    )
    run = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p",
         "tests.support.leaks", str(tmp_path / "test_leaks.py"),
         "--rootdir", str(tmp_path), "--basetemp", str(tmp_path / "base")],
        capture_output=True, text=True, check=False, cwd=Path(__file__).parents[2],
    )  # fmt: skip
    marker = "leaked temporary folders: "
    named = [ln.removeprefix(marker) for ln in run.stderr.splitlines() if ln.startswith(marker)]
    for folder in ", ".join(named).split(", "):  # the folder the child leaked: not ours to keep
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
    assert named, run.stdout + run.stderr
    assert run.returncode == pytest.ExitCode.TESTS_FAILED
