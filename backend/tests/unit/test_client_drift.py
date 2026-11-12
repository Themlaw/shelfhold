"""The typed API client is verified in CI, never edited (EF-INT-001, M3 L02).

`tools/client_drift.py` runs after `open-api/generate.sh`; it fails when the regenerated schema or
client differ from the committed ones, or when a generated file appeared that nobody committed."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.req("EF-INT-001")


def _drift():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("client_drift", ROOT / "tools" / "client_drift.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "open-api").mkdir()
    (tmp_path / "open-api/openapi.json").write_text("{}\n", encoding="utf-8")
    client = tmp_path / "frontend/src/lib/api"
    client.mkdir(parents=True)
    (client / "sdk.gen.ts").write_text("export const a = 1;\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_typed_api_client_regenerated_no_diff(tmp_path: Path) -> None:
    drift = _drift()
    repo = _repo(tmp_path)
    assert drift.drifted(repo) == []
    # a regenerated file that differs from the committed one is drift
    (repo / "frontend/src/lib/api/sdk.gen.ts").write_text("export const a = 2;\n", encoding="utf-8")
    assert drift.drifted(repo) == ["frontend/src/lib/api/sdk.gen.ts"]
    _git(repo, "checkout", "--", ".")
    # so is a file the generator produced that was never committed
    (repo / "frontend/src/lib/api/new.gen.ts").write_text("export {};\n", encoding="utf-8")
    assert drift.drifted(repo) == ["frontend/src/lib/api/new.gen.ts"]
    (repo / "frontend/src/lib/api/new.gen.ts").unlink()
    # and an edited schema
    (repo / "open-api/openapi.json").write_text('{"a": 1}\n', encoding="utf-8")
    assert drift.drifted(repo) == ["open-api/openapi.json"]


def test_typed_api_client_drift_cli_exit_code(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run = lambda: subprocess.run(  # noqa: E731
        [sys.executable, str(ROOT / "tools/client_drift.py"), str(repo)],
        capture_output=True,
        text=True,
    )
    assert run().returncode == 0
    (repo / "open-api/openapi.json").write_text('{"a": 1}\n', encoding="utf-8")
    done = run()
    assert done.returncode == 1
    assert "open-api/openapi.json" in done.stdout
