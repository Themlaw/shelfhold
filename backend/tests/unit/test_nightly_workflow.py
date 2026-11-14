"""The nightly workflow keeps its promises (ENF-LEG-013, ENF-COMP-001): offline checks of
`nightly.yml` and of the files it runs. Not required to merge, pinned, minimal, no real source."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
EXTENSION = ROOT / "backend" / "tests" / "contract" / "suwayomi-synthetic-extension"
SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _nightly() -> dict[str, Any]:
    loaded = yaml.safe_load((WORKFLOWS / "nightly.yml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    # a job that calls a reusable workflow has no steps of its own: the pins of `image.yml` are
    # checked with that file (the call itself is checked below)
    return [s for job in workflow["jobs"].values() for s in job.get("steps", [])]


@pytest.mark.req("ENF-LEG-013")
def test_nightly_is_scheduled_and_never_triggered_by_a_pull_request() -> None:
    triggers = _nightly()[True]  # YAML 1.1 reads the key `on` as a boolean
    assert set(triggers) == {"workflow_dispatch", "schedule"}


@pytest.mark.req("ENF-LEG-013")
def test_nightly_is_not_a_required_check() -> None:
    ci = (WORKFLOWS / "ci.yml").read_text("utf-8")
    assert "nightly" not in ci
    success = yaml.safe_load(ci)["jobs"]["ci-success"]["needs"]
    assert not [n for n in success if "nightly" in n]


@pytest.mark.req("ENF-LEG-013")
def test_nightly_actions_are_pinned_by_sha_and_permissions_are_minimal() -> None:
    workflow = _nightly()
    assert workflow["permissions"] == {}
    for name, job in workflow["jobs"].items():
        assert job["permissions"] == {"contents": "read"}, name
    for step in _steps(workflow):
        if "uses" in step:
            assert SHA.match(step["uses"]), step["uses"]
            if step["uses"].startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False


def test_nightly_runs_the_image_journeys_on_both_architectures() -> None:
    call = _nightly()["jobs"]["image-journeys"]
    assert call["uses"] == "./.github/workflows/image.yml"  # the file of this repository, not a ref
    assert call["with"] == {"arm64": True}  # a night builds and tests amd64 and arm64
    assert "cache-write" not in call["with"]  # and never refreshes the layer cache


def test_nightly_runs_kavita_on_the_line_the_pinned_version_and_the_latest() -> None:
    matrix = _nightly()["jobs"]["kavita"]["strategy"]["matrix"]["include"]
    by_label = {entry["label"]: entry["image"] for entry in matrix}
    assert set(by_label) == {"line-0.9.1", "pinned", "latest"}
    assert by_label["line-0.9.1"].endswith("/kavita:0.9.1")
    assert re.search(r"@sha256:[0-9a-f]{64}$", by_label["pinned"])
    assert by_label["latest"].endswith("/kavita:latest")
    assert all(image.startswith("docker.io/jvmilazz0/kavita") for image in by_label.values())


@pytest.mark.req("ENF-LEG-013")
def test_synthetic_extension_has_no_third_party_source_and_a_pinned_fixture_image() -> None:
    compose = yaml.safe_load((EXTENSION / "compose.yaml").read_text("utf-8"))
    assert "@sha256:" in compose["services"]["fixtures"]["image"]
    assert "ports" not in compose["services"]["fixtures"]  # reachable on the internal network only
    base = yaml.safe_load((ROOT / "e2e" / "suwayomi" / "compose.yaml").read_text("utf-8"))
    assert base["networks"]["contract"]["internal"] is True
    assert base["services"]["suwayomi"]["environment"]["EXTENSION_STORES"] == "[]"
    hosts = set()
    for path in [*EXTENSION.rglob("*.kt"), *EXTENSION.rglob("*.py"), *EXTENSION.rglob("*.kts")]:
        hosts |= set(re.findall(r"https?://([A-Za-z0-9.-]+)", path.read_text("utf-8")))
    assert hosts <= {"fixtures", "schemas.android.com", "jitpack.io"}, hosts
