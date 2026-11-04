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


def test_ci_uses_registry_cache(image_wf: dict[str, Any]) -> None:
    step = build_steps(image_wf["jobs"]["build"])[0]["with"]
    assert step["cache-from"] == "type=registry,ref=${{ env.IMAGE }}-cache:${{ matrix.arch }}"
    # cache-to exists, is a registry cache in mode=max, and only when the caller allows writing
    cache_to = step["cache-to"]
    assert "inputs.cache-write" in cache_to
    assert "type=registry,ref={0}-cache:{1},mode=max" in cache_to
    for workflow in WORKFLOWS.glob("*.yml"):
        assert "type=gha" not in workflow.read_text(
            encoding="utf-8"
        )  # never the 10 GB Actions cache


def test_cache_is_read_with_a_login_except_on_fork_pull_requests(image_wf: dict[str, Any]) -> None:
    login = next(
        s for s in steps_of(image_wf["jobs"]["build"]) if "login-action" in s.get("uses", "")
    )
    assert "head.repo.full_name == github.repository" in login["if"]
    assert login["with"]["password"] == "${{ github.token }}"  # noqa: S105


def test_image_builds_natively_on_both_architectures_without_qemu() -> None:
    for name, jobs in (("image.yml", ("build", "e2e")), ("image-publish.yml", ("publish",))):
        wf = load(WORKFLOWS / name)
        for job in jobs:
            runners = {i["runner"] for i in wf["jobs"][job]["strategy"]["matrix"]["include"]}
            assert runners == {"ubuntu-24.04", "ubuntu-24.04-arm"}, f"{name}:{job}"
        assert "qemu" not in (WORKFLOWS / name).read_text(encoding="utf-8").lower()


def test_published_image_is_the_tested_archive_never_a_rebuild() -> None:
    publish = load(WORKFLOWS / "image-publish.yml")
    text = (WORKFLOWS / "image-publish.yml").read_text(encoding="utf-8")
    assert not [
        s for s in steps_of(publish["jobs"]["publish"]) if "build-push-action" in s.get("uses", "")
    ]
    assert "skopeo copy" in text and "oci-archive:" in text
    assert "differs from the tested" in text  # the pushed digest is compared with the tested one
    e2e = [
        s
        for s in steps_of(load(WORKFLOWS / "image.yml")["jobs"]["e2e"])
        if "download-artifact" in s.get("uses", "")
    ]
    assert (
        e2e[0]["with"]["name"] == "image-${{ matrix.arch }}"
    )  # e2e loads the archive built on that arch


def test_image_is_pushed_by_digest_on_main_only() -> None:
    ci = load(WORKFLOWS / "ci.yml")["jobs"]
    main_only = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert ci["image-main"]["if"] == main_only and ci["image-publish"]["if"] == main_only
    assert ci["image-publish"]["needs"] == "image-main"  # published only after the e2e of the run
    assert ci["image-pr"]["if"] == "github.event_name == 'pull_request'"
    merge = load(WORKFLOWS / "image-publish.yml")["jobs"]["merge"]
    assert merge["needs"] == "publish"


def _calls(job: dict[str, Any]) -> Path | None:
    uses = job.get("uses")
    return ROOT / uses[2:] if isinstance(uses, str) and uses.startswith("./") else None


def _writes_packages(workflow: Path) -> bool:
    jobs = load(workflow)["jobs"]
    return any((j.get("permissions") or {}).get("packages") == "write" for j in jobs.values())


def test_no_job_reachable_on_pull_request_has_packages_write() -> None:
    """A pull_request run, even from a branch of this repository, never gets packages: write."""
    ci = load(WORKFLOWS / "ci.yml")
    assert (ci.get("permissions") or {}).get("packages") != "write"
    for name, job in ci["jobs"].items():
        condition = str(job.get("if", ""))
        reachable_on_pr = (
            "github.event_name == 'push'" not in condition
            and "== 'refs/heads/main'" not in condition
        )
        callee = _calls(job)
        writes = (job.get("permissions") or {}).get("packages") == "write" or (
            callee is not None and _writes_packages(callee)
        )
        if reachable_on_pr:
            assert not writes, f"ci.yml job {name} can write packages on pull_request"
            assert (job.get("permissions") or {}).get("packages") in (None, "read", "none")
    # a reachable-on-PR callee must not declare write jobs either (it would fail at startup)
    assert not _writes_packages(WORKFLOWS / "image.yml")


def test_packages_write_only_on_jobs_that_push() -> None:
    publish = load(WORKFLOWS / "image-publish.yml")
    assert {
        n
        for n, j in publish["jobs"].items()
        if (j.get("permissions") or {}).get("packages") == "write"
    } == {
        "publish",
        "merge",
    }
    assert publish["permissions"] == {"contents": "read"}
    assert (load(WORKFLOWS / "ci.yml")["jobs"]["image-pr"]["permissions"]) == {
        "contents": "read",
        "packages": "read",
    }


def test_concurrency_cancels_only_pull_requests() -> None:
    assert load(WORKFLOWS / "ci.yml")["concurrency"]["cancel-in-progress"] == (
        "${{ github.event_name == 'pull_request' }}"
    )


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
    assert carriers == {
        ".github/workflows/image.yml",
        ".github/workflows/image-publish.yml",
        "compose.yaml",
    }
    for name in ("image.yml", "image-publish.yml"):
        assert load(WORKFLOWS / name)["env"]["IMAGE"] == IMAGE_NAME


def test_image_size_gate_is_300_mb_and_ci_requires_the_image_jobs(image_wf: dict[str, Any]) -> None:
    assert image_wf["env"]["IMAGE_MAX_BYTES"] == "300000000"
    runs = " ".join(str(s.get("run", "")) for s in steps_of(image_wf["jobs"]["build"]))
    assert "check-image-size.sh" in runs
    ci = load(WORKFLOWS / "ci.yml")
    assert {"image-pr", "image-main", "image-publish"} <= set(ci["jobs"]["ci-success"]["needs"])
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "IMAGE_PR" in text and "IMAGE_MAIN" in text and "IMAGE_PUBLISH" in text


def test_image_workflow_is_reusable(image_wf: dict[str, Any]) -> None:
    assert "workflow_call" in image_wf[True]  # YAML parses the key `on` as True
    assert "workflow_call" in load(WORKFLOWS / "image-publish.yml")[True]


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
