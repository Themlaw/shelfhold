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
