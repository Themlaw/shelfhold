"""Static checks of the image pipeline: ENF-SEC-006 (compose lint), ENF-QUAL-007 (registry cache),
ENF-PORT-001 (native multi-arch), ENF-PERF-002 (size gate)"""

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


def load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def image_wf() -> dict[str, Any]:
    return load(WORKFLOWS / "image.yml")


def steps_of(job: dict[str, Any]) -> list[dict[str, Any]]:
    return list(job["steps"])


def build_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in steps_of(job) if str(s.get("uses", "")).startswith("docker/build-push")]


# --- compose (ENF-SEC-006) ---------------------------------------------------------------


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return load(ROOT / "compose.yaml")


@pytest.mark.req("ENF-SEC-006")
def test_compose_runtime_service_is_hardened(compose: dict[str, Any]) -> None:
    svc = compose["services"]["shelfhold"]
    assert svc["user"] == "1000:1000"
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["init"] is True
    assert svc["stop_grace_period"] == "60s"
    assert any(t.startswith("/tmp:") and "size=256m" in t for t in svc["tmpfs"])  # noqa: S108
    assert svc["mem_limit"]
    assert int(svc["pids_limit"]) > 0
    assert svc["ulimits"]["core"] == 0
    options = svc["logging"]["options"]
    assert options["max-size"] and options["max-file"]
    health = svc["healthcheck"]
    assert health["start_period"] and health["test"]


@pytest.mark.req("ENF-SEC-006", "ENF-OPS-002")
def test_compose_migrate_runs_first_and_is_hardened_too(compose: dict[str, Any]) -> None:
    migrate = compose["services"]["shelfhold-migrate"]
    assert migrate["command"] == ["shelfhold", "migrate"]
    assert migrate["read_only"] is True
    assert migrate["cap_drop"] == ["ALL"]
    assert migrate["restart"] == "no"
    depends = compose["services"]["shelfhold"]["depends_on"]["shelfhold-migrate"]
    assert depends["condition"] == "service_completed_successfully"


@pytest.mark.req("ENF-OPS-012")
def test_compose_has_distinct_data_and_store_volumes(compose: dict[str, Any]) -> None:
    for name in ("shelfhold", "shelfhold-migrate"):
        pairs = [v.split(":")[:2] for v in compose["services"][name]["volumes"]]
        assert sorted(target for _, target in pairs) == ["/data", "/store"]
        assert len({source for source, _ in pairs}) == 2


# --- Dockerfile ----------------------------------------------------------------------------


@pytest.mark.req("ENF-SEC-006")
def test_dockerfile_is_pinned_unprivileged_and_healthchecked() -> None:
    text = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    base_images = re.findall(r"^FROM (\S+)", text, flags=re.MULTILINE)
    external = [f for f in base_images if "/" in f or ":" in f]
    assert external and all(
        re.search(r"@sha256:[0-9a-f]{64}$", f) for f in external if "$" not in f
    )
    assert re.search(r"^USER 1000:1000$", text, flags=re.MULTILINE)
    assert "HEALTHCHECK" in text
    assert "MALLOC_ARENA_MAX=2" in text
    assert "SHELFHOLD_HOST=0.0.0.0" in text
    assert "python:3.13-slim@sha256:" in text
    assert re.match(r"# syntax=docker/dockerfile:[\d.]+@sha256:[0-9a-f]{64}\n", text)
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "frontend/.env*" in ignore


# --- workflows -------------------------------------------------------------------------------


@pytest.mark.req("ENF-QUAL-007")
def test_ci_uses_registry_cache(image_wf: dict[str, Any]) -> None:
    step = build_steps(image_wf["jobs"]["build"])[0]["with"]
    assert "type=registry,ref={0}-cache:{1}" in step["cache-from"]
    assert "inputs.cache-write" in step["cache-to"]  # only writable when the caller allows it
    for workflow in WORKFLOWS.glob("*.yml"):
        assert "type=gha" not in workflow.read_text(encoding="utf-8")  # never the Actions cache


@pytest.mark.req("ENF-PORT-001")
def test_image_is_published_natively_on_both_architectures_without_qemu() -> None:
    publish = load(WORKFLOWS / "image-publish.yml")
    runners = {i["runner"] for i in publish["jobs"]["publish"]["strategy"]["matrix"]["include"]}
    assert runners == {"ubuntu-24.04", "ubuntu-24.04-arm"}
    for name in ("image.yml", "image-publish.yml"):
        assert "qemu" not in (WORKFLOWS / name).read_text(encoding="utf-8").lower()


# --- size script, with a stubbed docker -----------------------------------------------------


def _run_size_check(
    tmp_path: Path, payload_bytes: int, limit: int
) -> subprocess.CompletedProcess[str]:
    stub = tmp_path / "docker"
    stub.write_text(
        f"#!/bin/sh\nhead -c {payload_bytes} /dev/zero | tr '\\0' 'a'\n", encoding="utf-8"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    script = ROOT / "docker" / "check-image-size.sh"
    return subprocess.run(  # noqa: S603
        [str(script), "img", str(limit)], capture_output=True, text=True, env=env, check=False
    )


@pytest.mark.req("ENF-PERF-002")
def test_size_gate_passes_under_the_limit(tmp_path: Path) -> None:
    assert _run_size_check(tmp_path, 10_000, 1_000).returncode == 0


@pytest.mark.req("ENF-PERF-002")
def test_size_gate_fails_over_the_limit(tmp_path: Path) -> None:
    result = _run_size_check(tmp_path, 100_000, 10)
    assert result.returncode == 1
    assert "ENF-PERF-002" in result.stdout
