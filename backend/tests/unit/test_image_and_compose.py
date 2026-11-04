"""Static checks of the image pipeline: ENF-SEC-006 (compose lint), ENF-QUAL-007 (registry cache),
ENF-PORT-001 (native multi-arch), ENF-PERF-002 (size gate), single definition of the image name."""

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
IMAGE_NAME = "ghcr.io/themlaw/shelfhold"


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


def test_compose_migrate_runs_first_and_is_hardened_too(compose: dict[str, Any]) -> None:
    migrate = compose["services"]["shelfhold-migrate"]
    assert migrate["command"] == ["shelfhold", "migrate"]
    assert migrate["read_only"] is True
    assert migrate["cap_drop"] == ["ALL"]
    assert migrate["restart"] == "no"
    depends = compose["services"]["shelfhold"]["depends_on"]["shelfhold-migrate"]
    assert depends["condition"] == "service_completed_successfully"


def test_compose_has_distinct_data_and_store_volumes(compose: dict[str, Any]) -> None:
    for name in ("shelfhold", "shelfhold-migrate"):
        pairs = [v.split(":")[:2] for v in compose["services"][name]["volumes"]]
        assert sorted(target for _, target in pairs) == ["/data", "/store"]
        assert len({source for source, _ in pairs}) == 2


def test_compose_image_is_edge_on_ghcr(compose: dict[str, Any]) -> None:
    assert compose["services"]["shelfhold"]["image"] == f"{IMAGE_NAME}:edge"


def test_e2e_compose_is_hardened_and_image_is_injected() -> None:
    e2e = load(ROOT / "e2e" / "compose.yaml")
    for name in ("shelfhold", "shelfhold-migrate"):
        svc = e2e["services"][name]
        assert svc["user"] == "1000:1000" and svc["read_only"] is True
        assert svc["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in svc["security_opt"]
        assert svc["network_mode"] == "service:egress-watch"
    assert "SHELFHOLD_IMAGE" in e2e["services"]["shelfhold"]["image"]
    assert IMAGE_NAME not in (ROOT / "e2e" / "compose.yaml").read_text(encoding="utf-8")


def test_dev_compose_builds_the_dev_target() -> None:
    dev = load(ROOT / "compose.dev.yaml")
    assert dev["services"]["backend"]["build"]["target"] == "dev"


# --- Dockerfile ----------------------------------------------------------------------------


def test_dockerfile_is_pinned_unprivileged_and_healthchecked() -> None:
    text = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    froms = re.findall(r"^FROM (\S+)", text, flags=re.MULTILINE)
    external = [f for f in froms if "/" in f or ":" in f]
    assert external and all(
        re.search(r"@sha256:[0-9a-f]{64}$", f) for f in external if "$" not in f
    )
    assert re.search(r"^USER 1000:1000$", text, flags=re.MULTILINE)
    assert "HEALTHCHECK" in text
    assert "MALLOC_ARENA_MAX=2" in text
    assert "SHELFHOLD_HOST=0.0.0.0" in text
    assert "python:3.13-slim@sha256:" in text


# --- workflows -------------------------------------------------------------------------------


def test_ci_uses_registry_cache(image_wf: dict[str, Any]) -> None:
    pushed = build_steps(image_wf["jobs"]["publish"])[0]["with"]
    assert re.fullmatch(
        r"type=registry,ref=\$\{\{ env\.IMAGE \}\}-cache:\$\{\{ matrix\.arch \}\},mode=max",
        pushed["cache-to"],
    )
    for job in ("build", "publish"):
        step = build_steps(image_wf["jobs"][job])[0]["with"]
        assert step["cache-from"].startswith("type=registry,ref=${{ env.IMAGE }}-cache:")
    text = (WORKFLOWS / "image.yml").read_text(encoding="utf-8")
    assert "type=gha" not in text  # ENF-QUAL-007: never the 10 GB Actions cache
    assert "cache-to" not in yaml.safe_dump(
        image_wf["jobs"]["build"]
    )  # a PR never writes the cache


def test_image_builds_natively_on_both_architectures_without_qemu(
    image_wf: dict[str, Any],
) -> None:
    for job in ("build", "publish"):
        runners = {i["runner"] for i in image_wf["jobs"][job]["strategy"]["matrix"]["include"]}
        assert runners == {"ubuntu-24.04", "ubuntu-24.04-arm"}
    assert "qemu" not in (WORKFLOWS / "image.yml").read_text(encoding="utf-8").lower()


def test_image_is_pushed_by_digest_on_main_only(image_wf: dict[str, Any]) -> None:
    jobs = image_wf["jobs"]
    assert jobs["publish"]["if"] == "inputs.push" and jobs["merge"]["if"] == "inputs.push"
    assert "push-by-digest=true" in build_steps(jobs["publish"])[0]["with"]["outputs"]
    assert build_steps(jobs["build"])[0]["with"]["push"] is False
    ci = load(WORKFLOWS / "ci.yml")
    assert ci["jobs"]["image"]["with"]["push"] == "${{ github.event_name == 'push' }}"
    assert "e2e" in jobs["merge"]["needs"]  # edge only moves once the image passed e2e


def test_packages_write_only_on_jobs_that_push(image_wf: dict[str, Any]) -> None:
    writers = {
        name
        for name, job in image_wf["jobs"].items()
        if (job.get("permissions") or {}).get("packages") == "write"
    }
    assert writers == {"publish", "merge"}
    assert image_wf["permissions"] == {"contents": "read"}


def test_docker_actions_are_pinned_by_full_sha() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        for use in re.findall(r"uses: (\S+)", workflow.read_text(encoding="utf-8")):
            if use.startswith("./"):
                continue
            assert re.search(r"@[0-9a-f]{40}$", use), f"{workflow.name}: {use}"


def test_image_name_is_defined_in_one_place() -> None:
    carriers = {
        str(p.relative_to(ROOT))
        for p in [*WORKFLOWS.glob("*.yml"), ROOT / "compose.yaml", ROOT / "e2e" / "compose.yaml"]
        if IMAGE_NAME in p.read_text(encoding="utf-8")
    }
    assert carriers == {".github/workflows/image.yml", "compose.yaml"}
    assert load(WORKFLOWS / "image.yml")["env"]["IMAGE"] == IMAGE_NAME


def test_image_size_gate_is_300_mb_and_ci_requires_the_image_job(image_wf: dict[str, Any]) -> None:
    assert image_wf["env"]["IMAGE_MAX_BYTES"] == "300000000"
    runs = " ".join(str(s.get("run", "")) for s in steps_of(image_wf["jobs"]["build"]))
    assert "check-image-size.sh" in runs
    ci = load(WORKFLOWS / "ci.yml")
    assert "image" in ci["jobs"]["ci-success"]["needs"]
    assert "IMAGE_RESULT" in (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")


def test_image_workflow_is_reusable(image_wf: dict[str, Any]) -> None:
    assert "workflow_call" in image_wf[True]  # YAML parses the key `on` as True


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


def test_size_gate_passes_under_the_limit(tmp_path: Path) -> None:
    assert _run_size_check(tmp_path, 10_000, 1_000).returncode == 0


def test_size_gate_fails_over_the_limit(tmp_path: Path) -> None:
    result = _run_size_check(tmp_path, 100_000, 10)
    assert result.returncode == 1
    assert "ENF-PERF-002" in result.stdout
