"""CI test split: tests/{property,golden,faults} exist, carry their marker and have their own job."""

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

BACKEND = Path(__file__).resolve().parents[2]
CI = BACKEND.parent / ".github" / "workflows" / "ci.yml"
SUITES = ("property", "golden", "faults")


@pytest.mark.req("ENF-QUAL-006")
@pytest.mark.parametrize("suite", SUITES)
def test_suite_directory_marker_and_tests_exist(suite: str) -> None:
    assert (BACKEND / "tests" / suite / "__init__.py").is_file()
    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith(f"{suite}:") for m in markers)
    # the marker is applied from the directory: `-m <suite>` selects exactly that directory
    out = subprocess.run(
        [sys.executable, "-m", "pytest", f"tests/{suite}", "-m", suite, "--collect-only", "-q"],
        cwd=BACKEND, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"tests/{suite}/" in out.stdout


@pytest.mark.req("ENF-QUAL-006")
def test_ci_runs_property_golden_and_faults_in_a_matrix_aggregated_by_ci_success() -> None:
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = ci["jobs"]
    job = jobs["backend-suites"]
    assert job["if"] == "needs.changes.outputs.backend == 'true'"
    assert list(job["strategy"]["matrix"]["suite"]) == list(SUITES)
    assert job["strategy"]["fail-fast"] is False
    assert {"backend-suites"} <= set(jobs["coverage"]["needs"])
    assert "backend-suites" in jobs["ci-success"]["needs"]
    assert "tests/${{ matrix.suite }}" in CI.read_text(encoding="utf-8")
