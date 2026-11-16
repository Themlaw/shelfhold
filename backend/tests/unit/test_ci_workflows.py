"""ci.yml aggregation: the required check `ci-success` must depend on every gate."""

import fnmatch
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CI = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"


def _filters(ci: dict[str, Any]) -> dict[str, list[str]]:
    step = next(s for s in ci["jobs"]["changes"]["steps"] if s.get("id") == "filter")
    assert step["with"]["predicate-quantifier"] == "some-with-excludes"
    loaded: dict[str, list[str]] = yaml.safe_load(step["with"]["filters"])
    return loaded


def _glob(pattern: str) -> re.Pattern[str]:
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def _matches(patterns: list[str], path: str) -> bool:
    """dorny/paths-filter, predicate-quantifier some-with-excludes."""
    positive = [p for p in patterns if not p.startswith("!")]
    negative = [p[1:] for p in patterns if p.startswith("!")]
    return any(_glob(p).match(path) for p in positive) and not any(
        _glob(p).match(path) for p in negative
    )


def _scope(files: list[str], event: str = "pull_request") -> dict[str, str]:
    """Runs the real `scope` step of the `changes` job on the filters computed for `files`."""
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps = ci["jobs"]["changes"]["steps"]
    script = next(s for s in steps if s.get("id") == "scope")["run"]
    outputs = {
        name: str(any(_matches(patterns, f) for f in files)).lower()
        for name, patterns in _filters(ci).items()
    }
    bash = shutil.which("bash")
    assert bash and shutil.which("jq")
    done = subprocess.run(
        [bash, "-c", 'GITHUB_OUTPUT=$(mktemp); export GITHUB_OUTPUT; . /dev/stdin >/dev/null; cat "$GITHUB_OUTPUT"'],
        input=script, capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin", "EVENT": event, "FILTERS": json.dumps(outputs)},
    )  # fmt: skip
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


def _runs(files: list[str], event: str = "pull_request") -> set[str]:
    """The gated jobs of ci.yml that run for these changed files."""
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    out = _scope(files, event)
    ran: set[str] = set()
    for name, job in ci["jobs"].items():
        cond = str(job.get("if", ""))
        found = re.fullmatch(r"needs\.changes\.outputs\.(\w+) == 'true'", cond)
        if found and out[found.group(1)] == "true":
            ran.add(name)
        if name == "image-pr" and out["image_build"] == "true" and event == "pull_request":
            ran.add(name)
    return ran


BACKEND_ALL = {"backend-unit", "backend-integration", "backend-suites", "backend-bench", "coverage", "fixtures"}


@pytest.mark.req("ENF-SEC-007")
def test_pull_request_scope_production_code_runs_everything_as_before() -> None:
    for path in (
        "backend/src/shelfhold/app.py",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/tests/conftest.py",
        "backend/tests/library_env.py",
        "backend/tests/fixtures/synthetic/a.cbz",
        "docker/Dockerfile",
        ".github/workflows/ci.yml",
    ):
        assert BACKEND_ALL <= _runs([path]), path
    assert {"frontend", "openapi", "e2e-front", "frontend-e2e", "client-drift"} <= _runs(["frontend/x.ts"])
    assert {"image-pr"} <= _runs(["backend/src/shelfhold/app.py"])
    full = _scope(["backend/src/shelfhold/app.py"])
    assert json.loads(full["backend_suites"]) == ["property", "golden", "faults"]


@pytest.mark.req("ENF-SEC-007")
def test_pull_request_scope_tests_of_one_suite_run_that_suite_only() -> None:
    assert _runs(["backend/tests/unit/test_x.py"]) == {"backend-unit", "fixtures"}
    assert _runs(["backend/tests/integration/test_x.py"]) == {"backend-integration"}
    assert _runs(["backend/tests/bench/test_x.py"]) == {"backend-bench"}
    for suite in ("golden", "faults", "property"):
        files = [f"backend/tests/{suite}/test_x.py"]
        assert _runs(files) == {"backend-suites"}, suite
        assert json.loads(_scope(files)["backend_suites"]) == [suite]
    mixed = ["backend/tests/unit/a.py", "backend/tests/golden/b.py"]
    assert _runs(mixed) == {"backend-unit", "fixtures", "backend-suites"}
    assert json.loads(_scope(mixed)["backend_suites"]) == ["golden"]


@pytest.mark.req("ENF-SEC-007")
def test_pull_request_scope_compose_runs_unit_and_the_install_smoke_only() -> None:
    for path in ("compose.yaml", "compose.dev.yaml", ".env.example", ".dockerignore"):
        assert _runs([path]) == {"backend-unit", "image-pr"}, path
        assert _scope([path])["image"] == "false"  # image-pr is then called with only-smoke
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert ci["jobs"]["image-pr"]["with"]["only-smoke"] == "${{ needs.changes.outputs.image != 'true' }}"
    image = yaml.safe_load((CI.parent / "image.yml").read_text(encoding="utf-8"))
    assert image["on"]["workflow_call"]["inputs"]["only-smoke"]["default"] is False
    assert "inputs.only-smoke && '[\"smoke-install\"]'" in (CI.parent / "image.yml").read_text(encoding="utf-8")


@pytest.mark.req("ENF-SEC-007")
def test_pull_request_scope_docs_only_runs_no_test_job() -> None:
    for path in ("README.md", "CONTRIBUTING.md", "docs/adr/0001.md", "backend/README.md"):
        assert _runs([path]) == set(), path


@pytest.mark.req("ENF-SEC-007")
def test_push_to_main_runs_everything() -> None:
    out = _scope(["README.md"], event="push")
    assert all(v == "true" for k, v in out.items() if k != "backend_suites")
    assert json.loads(out["backend_suites"]) == ["property", "golden", "faults"]


@pytest.mark.req("ENF-SEC-007")
def test_ci_success_tolerates_a_skip_only_when_the_scope_says_so() -> None:
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    gate = ci["jobs"]["ci-success"]["steps"][0]["run"]
    for job, flag in (
        ("BACKEND_UNIT", "UNIT_CHANGED"),
        ("BACKEND_INTEGRATION", "INTEGRATION_CHANGED"),
        ("BACKEND_SUITES", "SUITES_CHANGED"),
        ("BACKEND_BENCH", "BENCH_CHANGED"),
        ("COVERAGE", "BACKEND_CHANGED"),
        ("FIXTURES", "FIXTURES_CHANGED"),
        ("IMAGE_PR", "IMAGE_CHANGED"),
    ):
        assert f'check_filtered "${job}" "${flag}"' in gate, job
    # coverage needs the data of every suite: it is gated on production code, never on tests alone
    assert ci["jobs"]["coverage"]["if"] == "needs.changes.outputs.backend == 'true'"


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
def test_traceability_blocks_every_delivered_milestone() -> None:
    """M2 and M3 block like the others since the closure of M3 (L58): no milestone is exempted."""
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    run = next(
        s["run"]
        for s in ci["jobs"]["traceability"]["steps"]
        if "run" in s and "traceability.py" in s["run"]
    )
    assert "for ms in M0 M1a M1b M2 M3 M4; do" in run
    assert "case " not in run, "a milestone is exempted from the blocking status"
    assert "[ $code -eq 0 ] || status=1" in run


@pytest.mark.req("ENF-SEC-007")
def test_traceability_reports_m4_without_blocking() -> None:
    """M4 is in the matrix and its summary, but only the delivered milestones fail the job."""
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    run = next(
        s["run"]
        for s in ci["jobs"]["traceability"]["steps"]
        if "run" in s and "traceability.py" in s["run"]
    )
    assert 'if [ "$ms" = M4 ]; then' in run
    block = run.split('if [ "$ms" = M4 ]; then')[1].split("else")[0]
    assert "status=1" not in block


@pytest.mark.req("ENF-SEC-007")
def test_image_paths_filter_covers_every_dockerfile_copy_source() -> None:
    root = CI.parents[2]
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    filters = _filters(ci)
    patterns = filters["image"] + filters["compose"]  # compose: .dockerignore, build + smoke only

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
    # a pull request runs what its files can change (test_pull_request_scope_*), a push everything
    assert jobs["coverage"]["if"] == "needs.changes.outputs.backend == 'true'"
    assert jobs["backend-unit"]["if"] == "needs.changes.outputs.unit == 'true'"
    assert jobs["backend-integration"]["if"] == "needs.changes.outputs.integration == 'true'"
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
