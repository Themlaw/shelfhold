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


@pytest.mark.req("ENF-SEC-007")
def test_the_image_is_built_with_the_version_of_the_package_for_its_oci_label() -> None:
    """Images are built once (image.yml); image-publish.yml and release.yml tag that digest. The
    label `org.opencontainers.image.version` therefore comes from the build argument."""
    workflow = (ROOT / ".github" / "workflows" / "image.yml").read_text(encoding="utf-8")
    assert "VERSION=${{ steps.version.outputs.version }}" in workflow
    command = re.search(r'run: echo "version=(\$\(.*\))" >> "\$GITHUB_OUTPUT"', workflow)
    assert command is not None
    shell = command.group(1)[2:-1]
    done = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", shell], cwd=ROOT, capture_output=True, text=True, check=True
    )
    package = re.search(r'^version = "(.*)"$', (ROOT / "backend/pyproject.toml").read_text(), re.M)
    assert package is not None and done.stdout.strip() == package.group(1) == "0.2.0"
    assert 'org.opencontainers.image.version="${VERSION}"' in (
        ROOT / "docker" / "Dockerfile"
    ).read_text(encoding="utf-8")
    for other in ("image-publish.yml", "release.yml"):  # they never rebuild
        text = (ROOT / ".github" / "workflows" / other).read_text(encoding="utf-8")
        assert "build-push-action" not in text and "docker build " not in text
