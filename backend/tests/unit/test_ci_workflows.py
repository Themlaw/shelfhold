"""ci.yml aggregation: the required check `ci-success` must depend on every gate."""

import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml

CI = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"


@pytest.mark.req("ENF-SEC-007")
def test_ci_success_needs_every_gate_including_dco_title_and_traceability() -> None:
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = ci["jobs"]
    assert {"dco", "conventional-title"} <= set(jobs)  # names required by the ruleset
    traceability = jobs["traceability"]
    assert not traceability.get("continue-on-error")
    needs = set(jobs["ci-success"]["needs"])
    assert needs >= {"lint", "denylist", "dco", "conventional-title", "traceability"}
    # every job of the workflow is aggregated, nothing can be forgotten
    assert needs == set(jobs) - {"ci-success"}
    assert jobs["ci-success"]["if"] == "always()"


@pytest.mark.req("ENF-SEC-007")
def test_image_paths_filter_covers_every_dockerfile_copy_source() -> None:
    root = CI.parents[2]
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    step = next(s for s in ci["jobs"]["changes"]["steps"] if s.get("id") == "filter")
    patterns = [
        p.lstrip("- ").strip("'\"")
        for p in step["with"]["filters"].split("image:")[1].splitlines()
        if p.strip().startswith("- ")
    ]

    def covered(path: str) -> bool:
        for pat in patterns:
            if pat.endswith("/**"):
                if path == pat[:-3] or path.startswith(pat[:-2]):
                    return True
            elif fnmatch.fnmatch(path, pat):
                return True
        return False

    sources: list[str] = []
    for line in (root / "docker" / "Dockerfile").read_text(encoding="utf-8").splitlines():
        words = line.split()
        if words[:1] == ["COPY"] and not any(w.startswith("--from") for w in words):
            sources += [w for w in words[1:-1] if not w.startswith("--")]
    assert sources
    assert ".dockerignore" in patterns
    uncovered = [s for s in sources if not covered(s.removeprefix("./"))]
    assert not uncovered, uncovered


@pytest.mark.req("ENF-SEC-007")
def test_backend_is_sharded_and_coverage_is_combined_with_unchanged_thresholds() -> None:
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = ci["jobs"]
    assert "backend" not in jobs  # replaced by the jobs below
    assert {"backend-unit", "backend-integration", "coverage"} <= set(jobs)
    assert set(jobs["coverage"]["needs"]) >= {"backend-unit", "backend-integration"}
    # same path filter as the former job: a backend-unrelated change skips all of them
    for name in ("backend-unit", "backend-integration", "coverage"):
        assert jobs[name]["if"] == "needs.changes.outputs.backend == 'true'"
    # the number of shards is defined once, in the `changes` job
    assert jobs["changes"]["env"]["BACKEND_SHARDS"].isdigit()
    assert jobs["changes"]["env"]["BACKEND_INTEGRATION_SHARDS"].isdigit()
    assert jobs["backend-integration"]["strategy"]["matrix"]["shard"]
    assert jobs["e2e-front"]["strategy"]["matrix"]["shard"]
    text = CI.read_text(encoding="utf-8")
    assert "--fail-under=85" in text  # diff coverage threshold kept
    pyproject = (CI.parents[2] / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    assert "fail_under = 80" in pyproject  # applied by `coverage report` in the coverage job


@pytest.mark.req("ENF-SEC-007")
def test_every_job_has_a_timeout_of_at_most_15_minutes() -> None:
    # backend-integration shards get 20 min of margin (target: < 7 min per shard)
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    for name, job in ci["jobs"].items():
        if "uses" in job:  # reusable workflow: its own jobs carry the timeout
            continue
        limit = 20 if name == "backend-integration" else 15
        assert 0 < job.get("timeout-minutes", 0) <= limit, name
    assert ci["concurrency"]["cancel-in-progress"]
