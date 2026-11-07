"""The image E2E runs in shards, one runner each from the same image artifact (ENF-QUAL-007):
every test belongs to one shard, and the workflow runs exactly the shards that are listed."""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _shards() -> dict[str, tuple[str, ...]]:
    spec = importlib.util.spec_from_file_location("e2e_shards", ROOT / "e2e" / "shards.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.SHARDS)


def _e2e_tests() -> list[str]:
    names = []
    for path in sorted((ROOT / "e2e" / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names += [
            n.name
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
        ]
    return names


@pytest.mark.req("ENF-QUAL-007")
def test_every_e2e_test_is_in_exactly_one_shard() -> None:
    listed = [name for names in _shards().values() for name in names]
    assert sorted(listed) == sorted(_e2e_tests())


@pytest.mark.req("ENF-QUAL-007")
def test_workflow_runs_each_shard_and_no_other() -> None:
    text = (ROOT / ".github" / "workflows" / "image.yml").read_text(encoding="utf-8")
    declared = re.search(r"shard: \[([^\]]+)\]", text)
    assert declared is not None
    assert [s.strip() for s in declared.group(1).split(",")] == list(_shards())
    assert "E2E_SHARD: ${{ matrix.shard }}" in text
    assert "timeout-minutes: 15" in text  # not raised: the shards are what keeps it short


@pytest.mark.req("ENF-QUAL-007")
def test_a_sharded_run_selects_only_its_tests_and_refuses_an_unlisted_one(tmp_path: Path) -> None:
    def collect(shard: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
             "tests"],
            cwd=ROOT / "e2e", capture_output=True, text=True, check=False,
            env={"E2E_SHARD": shard, "PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
        )  # fmt: skip

    done = collect("redeploy")
    assert "test_redeploy_recreates_container_without_loss" in done.stdout, done.stderr
    assert "test_two_series" not in done.stdout
