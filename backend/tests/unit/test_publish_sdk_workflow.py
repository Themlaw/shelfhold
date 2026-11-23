"""Publication of shelfhold-sdk (M6 L03, ENF-SEC-007): trusted publishing only.

The workflows are read as YAML, never run: `publish-sdk.yml` publishes by OIDC from a protected
environment — no long-lived token, no secret, one job holding `id-token` — and the `sdk-dist`
job of `ci.yml` is the dry run of every pull request: wheel and sdist built, metadata checked
without anything sent, the wheel imported where the host is absent, the conformance kit played
on the example plugins. Nothing is ever published by this repository's pull requests.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
# a dispatch ternary routes a manual run to its first value, a tag run to the second
TERNARY = re.compile(r"github\.event_name == 'workflow_dispatch' && '([^']*)' \|\| '([^']*)'")


def _routes(expression: str) -> tuple[str, str] | None:
    """(manual run, tag run) of the dispatch ternary, None when it is not one."""
    found = TERNARY.search(str(expression))
    return (found.group(1), found.group(2)) if found else None


def _load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    triggers = workflow.get(True) or workflow.get("on") or {}
    return triggers if isinstance(triggers, dict) else {k: {} for k in triggers}


def _uses(job: dict[str, Any]) -> list[str]:
    return [str(s["uses"]).split(" ")[0] for s in job.get("steps", []) if "uses" in s]


def _text(job: dict[str, Any]) -> str:
    return "\n".join(str(s.get("run", "")) for s in job.get("steps", []))


def _pypa_step(publish: dict[str, Any]) -> dict[str, Any]:
    return next(
        (s for s in publish.get("steps", []) if str(s.get("uses", "")).startswith("pypa/")), {}
    )


@pytest.mark.req("ENF-SEC-007")
def test_publish_sdk_workflow_without_long_lived_token() -> None:
    """D-P2: the OIDC token of the publish job is the one credential, nothing else exists."""
    text = (WORKFLOWS / "publish-sdk.yml").read_text(encoding="utf-8")
    assert "pypa/gh-action-pypi-publish@" in text  # the official action, nothing home-made
    workflow = _load("publish-sdk.yml")
    publish = workflow["jobs"]["publish"]
    # no secret of the repository is referenced anywhere: trusted publishing needs none, and a
    # token or a password on the action would silently disable it
    assert not re.search(r"secrets\.\w", text)
    step = _pypa_step(publish)
    assert PIN.match(_uses(publish)[-1])
    assert "password" not in (step.get("with") or {}) and "user" not in (step.get("with") or {})
    assert step["with"]["attestations"] is True  # PEP 740
    # the one credential is the OIDC token: it is the publish job's only permission
    assert publish["permissions"] == {"id-token": "write"}


@pytest.mark.req("ENF-SEC-007")
def test_publish_sdk_only_publish_job_has_id_token() -> None:
    workflow = _load("publish-sdk.yml")
    jobs = workflow["jobs"]
    assert {"build", "publish"} <= set(jobs)
    holders = [name for name, job in jobs.items() if "id-token" in (job.get("permissions") or {})]
    assert holders == ["publish"], holders
    # the build holds no permission at all: nothing it runs can elevate before publication
    assert jobs["build"].get("permissions") == {}
    # the build never publishes: the pypa action runs in the publish job only
    assert not [u for u in _uses(jobs["build"]) if "pypi-publish" in u]
    assert any("pypi-publish" in u for u in _uses(jobs["publish"]))
    # what is published is what was built: build hands the distributions over, publish receives them
    upload = next(
        s
        for s in jobs["build"]["steps"]
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    )
    download = next(
        s
        for s in jobs["publish"]["steps"]
        if str(s.get("uses", "")).startswith("actions/download-artifact@")
    )
    assert upload["with"]["name"] == download["with"]["name"]
    assert download["with"]["path"] == "dist/" and upload["with"]["if-no-files-found"] == "error"
    # the publish job runs no code of the repository (only the artifact travels)
    assert not [u for u in _uses(jobs["publish"]) if u.startswith("actions/checkout@")]


@pytest.mark.req("ENF-SEC-007")
def test_publish_sdk_environment_protected_and_tag_bound() -> None:
    workflow = _load("publish-sdk.yml")
    triggers = _on(workflow)
    # a tag sdk-v* alone triggers a publication; a manual run is the holder's dry run
    assert triggers.get("push", {}).get("tags") == ["sdk-v*"]
    assert "workflow_dispatch" in triggers
    assert "pull_request" not in triggers and "pull_request_target" not in triggers
    publish = workflow["jobs"]["publish"]
    # behind the protected environment: `pypi` when the tag triggered the run, `testpypi` when
    # the holder runs the workflow by hand. Both routes are compared exactly: a swapped
    # aiguillage would publish a manual run, from any ref, on the real index.
    assert isinstance(publish["environment"], dict)  # the map form carries the deployment card
    assert _routes(str(publish["environment"]["name"])) == ("testpypi", "pypi")
    assert _routes(str(_pypa_step(publish)["with"]["repository-url"])) == (
        "https://test.pypi.org/legacy/",
        "https://upload.pypi.org/legacy/",
    )
    # the tag is validated against the version it builds, before anything is published
    build_text = _text(workflow["jobs"]["build"])
    assert "sdk-v[0-9]" in build_text and "sdk/pyproject.toml" in build_text
    # a publication is never cancelled mid-flight by a newer run
    assert workflow["concurrency"]["group"] == "publish-sdk"
    assert workflow["concurrency"]["cancel-in-progress"] is False


@pytest.mark.req("ENF-SEC-007")
def test_sdk_dist_dry_run_runs_on_pull_requests() -> None:
    """The fallback of D-P2: every pull request proves the distribution it would publish."""
    ci = _load("ci.yml")
    job = ci["jobs"]["sdk-dist"]
    assert job["if"] == "needs.changes.outputs.sdk == 'true'"
    assert 0 < job["timeout-minutes"] <= 15
    # the scope of the job is what can change the distribution
    outputs = ci["jobs"]["changes"]["outputs"]
    assert outputs["sdk"] == "${{ steps.scope.outputs.sdk }}"
    filters = next(s for s in ci["jobs"]["changes"]["steps"] if s.get("id") == "filter")["with"][
        "filters"
    ]
    paths = yaml.safe_load(filters)["sdk"]
    for pattern in (
        "sdk/**",
        "pyproject.toml",
        "uv.lock",
        "docs/plugins/gabarit/**",
        "examples/**",
        ".github/workflows/publish-sdk.yml",
    ):
        assert pattern in paths, pattern
    scope = next(s for s in ci["jobs"]["changes"]["steps"] if s.get("id") == "scope")["run"]
    assert re.search(r'^\s*echo "sdk=\$\(any sdk\)"$', scope, re.M)
    # the required check depends on it, and tolerates a skip only when the scope says so
    assert "sdk-dist" in ci["jobs"]["ci-success"]["needs"]
    gate = ci["jobs"]["ci-success"]["steps"][0]
    assert gate["env"]["SDK_DIST"] == "${{ needs.sdk-dist.result }}"
    assert gate["env"]["SDK_CHANGED"] == "${{ needs.changes.outputs.sdk }}"
    assert 'check_filtered "$SDK_DIST" "$SDK_CHANGED"' in gate["run"]
    # the dry run builds, checks, installs — it never sends
    text = yaml.safe_dump(job)
    assert not re.search(r"uv publish|twine upload|pypi-publish", text)
    assert "id-token" not in (job.get("permissions") or {})
    runs = _text(job)
    assert "uv build --project sdk" in runs
    assert "METADATA" in runs and "PKG-INFO" in runs  # metadata read from the built files
    assert "uv venv" in runs and 'find_spec("shelfhold") is None' in runs
    # pytest is taken from the lockfile, never from a live resolution
    assert "uv export --project backend --locked" in runs and "^pytest==" in runs
    # the kit of the published wheel is played on the example plugins, without the host
    assert "docs/plugins/gabarit/tests" in runs
    assert "examples" in runs and "pytest" in runs


@pytest.mark.req("ENF-SEC-007")
def test_publish_sdk_holder_steps_are_documented() -> None:
    """D-P2 is a holder action: the steps outside the repository are written, or the
    requirement stays partial."""
    text = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    section = text.split("## Publier le SDK", 1)[-1]
    assert "## Publier le SDK" in text  # the section exists
    # the order the ADR asks for: TestPyPI first, PyPI at the closure
    assert "TestPyPI" in section and "testpypi" in section and "PyPI" in section
    assert "workflow_dispatch" in section and "sdk-v" in section
    # no API token is ever created: the trusted publisher and the protected environment are
    # the holder's settings, and the publication waits behind the holder's review
    assert "éditeur de confiance" in section
    assert "environnements protégés" in section
    assert "aucun jeton" in section
    # the environment and the tags are bound: a manual run or a branch never reaches PyPI
    assert "déploiement" in section.lower() and "sdk-v*" in section
    assert "refs/tags/sdk-v*" in section
    # a published number is never republished
    assert "ne se republie jamais" in section
    # without the holder's action, ENF-SEC-007 stays partial for the milestone
    assert "ENF-SEC-007" in section and "partielle" in section


# --- mutations: every way the publication could be given a credential, a wider token or a
# --- looser environment must be caught by the same properties


def _publish_problems(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if re.search(r"secrets\.\w", yaml.safe_dump(workflow)):
        problems.append("a secret is referenced: trusted publishing needs none")
    jobs = workflow["jobs"]
    for job_id, job in jobs.items():
        scopes = job.get("permissions") or {}
        if "id-token" in scopes and job_id != "publish":
            problems.append(f"{job_id}: the OIDC token on a job other than publish")
        if scopes.get("contents") == "write":
            problems.append(f"{job_id}: contents write")
    publish = jobs.get("publish")
    if not isinstance(publish, dict):
        return [*problems, "the publish job is gone"]
    if (publish.get("permissions") or {}) != {"id-token": "write"}:
        problems.append(f"publish permissions widened: {publish.get('permissions')}")
    publishing = [u for u in _uses(publish) if "pypi-publish" in u]
    if len(publishing) != 1 or not PIN.match(publishing[0]):
        problems.append(f"the official publish action, pinned by SHA, is missing: {_uses(publish)}")
    step = _pypa_step(publish)
    inputs = step.get("with") or {}
    if inputs.get("attestations") is not True:
        problems.append("the PEP 740 attestations are off")
    if "password" in inputs or "user" in inputs:
        problems.append("a username or password is passed: not trusted publishing")
    if "environment" not in publish:
        problems.append("the publish job runs outside any protected environment")
    # both routes are compared exactly: a swapped aiguillage would publish a manual run,
    # from any ref, on the real index
    environment = publish.get("environment")
    if isinstance(environment, dict):
        environment = environment.get("name")
    if _routes(str(environment or "")) != ("testpypi", "pypi"):
        problems.append("the environment does not route a manual run to testpypi and a tag to pypi")
    if _routes(str(inputs.get("repository-url", ""))) != (
        "https://test.pypi.org/legacy/",
        "https://upload.pypi.org/legacy/",
    ):
        problems.append("the endpoint does not route a manual run to TestPyPI and a tag to PyPI")
    triggers = _on(workflow)
    if triggers.get("push", {}).get("tags") != ["sdk-v*"]:
        problems.append(f"not bound to the sdk-v* tags: {triggers.get('push')}")
    if "pull_request" in triggers or "pull_request_target" in triggers:
        problems.append("a pull request can trigger the publication")
    build = jobs.get("build") or {}
    if "sdk-v" not in _text(build) or "pyproject.toml" not in _text(build):
        problems.append("the tag is no longer checked against the version it builds")
    steps = [str(s.get("name", "")) + str(s.get("run", "")) for s in build.get("steps", [])]
    after_build = steps[next((i for i, s in enumerate(steps) if "uv build" in s), 0) + 1 :]
    if not any("TAG" in s and ("dist/" in s or ".whl" in s) for s in after_build):
        problems.append("the tag is never checked against the built distributions")
    return problems


def _dry_run_problems(ci: dict[str, Any]) -> list[str]:
    job = ci["jobs"].get("sdk-dist")
    if not isinstance(job, dict):
        return ["the sdk-dist dry run is gone"]
    problems: list[str] = []
    text = yaml.safe_dump(job)
    if re.search(r"uv publish|twine upload|pypi-publish", text):
        problems.append("the dry run sends something")
    if "id-token" in (job.get("permissions") or {}):
        problems.append("the dry run holds the OIDC token")
    if "uv build --project sdk" not in _text(job):
        problems.append("the dry run no longer builds the distribution")
    if "METADATA" not in _text(job) or "PKG-INFO" not in _text(job):
        problems.append("the metadata is no longer checked")
    if 'find_spec("shelfhold") is None' not in _text(job):
        problems.append("the wheel is no longer proven to run without the host")
    if "docs/plugins/gabarit/tests" not in _text(job):
        problems.append("the kit is no longer played on the example plugins")
    if "examples" not in _text(job):
        problems.append("the example plugin of L10 is no longer wired")
    return problems


@pytest.mark.req("ENF-SEC-007")
def test_publish_sdk_mutations_are_caught() -> None:
    def run(mutate: Any) -> list[str]:
        workflow = copy.deepcopy(_load("publish-sdk.yml"))
        mutate(workflow)
        return _publish_problems(workflow)

    def secret(w: dict[str, Any]) -> None:
        w["jobs"]["publish"]["steps"][0]["env"] = {"TOKEN": "${{ secrets.PYPI_TOKEN }}"}

    assert any("secret" in p for p in run(secret))

    def token_on_build(w: dict[str, Any]) -> None:
        w["jobs"]["build"]["permissions"] = {"id-token": "write"}

    assert any("other than publish" in p for p in run(token_on_build))

    def contents_write(w: dict[str, Any]) -> None:
        w["jobs"]["build"]["permissions"] = {"contents": "write"}

    assert any("contents write" in p for p in run(contents_write))

    def no_environment(w: dict[str, Any]) -> None:
        w["jobs"]["publish"].pop("environment")

    assert any("protected environment" in p for p in run(no_environment))

    def no_attestations(w: dict[str, Any]) -> None:
        _pypa_step(w["jobs"]["publish"])["with"]["attestations"] = False

    assert any("attestations" in p for p in run(no_attestations))

    def by_password(w: dict[str, Any]) -> None:
        _pypa_step(w["jobs"]["publish"])["with"]["password"] = "synthetic-mutation-only"  # noqa: S105 - never a real credential

    assert any("password" in p for p in run(by_password))

    def any_tag(w: dict[str, Any]) -> None:
        w[True]["push"]["tags"] = ["*"]

    assert any("sdk-v" in p for p in run(any_tag))

    def by_pull_request(w: dict[str, Any]) -> None:
        w[True]["pull_request"] = {}

    assert any("pull request" in p for p in run(by_pull_request))

    def no_tag_check(w: dict[str, Any]) -> None:
        w["jobs"]["build"]["steps"] = [
            s for s in w["jobs"]["build"]["steps"] if "sdk-v" not in str(s.get("run", ""))
        ]

    assert any("checked against the version" in p for p in run(no_tag_check))

    def no_built_check(w: dict[str, Any]) -> None:
        steps = w["jobs"]["build"]["steps"]
        built_at = next(i for i, s in enumerate(steps) if "uv build" in str(s.get("run", "")))
        w["jobs"]["build"]["steps"] = [
            s for i, s in enumerate(steps) if i <= built_at or "TAG" not in str(s.get("run", ""))
        ]

    assert any("built distributions" in p for p in run(no_built_check))

    def swapped_routes(w: dict[str, Any]) -> None:
        # the adverse review's proof: an inverted aiguillage routes a manual run to the real
        # index — presence of both strings is not enough, the routes are compared
        publish = w["jobs"]["publish"]

        def swap(expression: str, first: str, second: str) -> str:
            return expression.replace(first, "\0").replace(second, first).replace("\0", second)

        publish["environment"]["name"] = swap(
            str(publish["environment"]["name"]), "'testpypi'", "'pypi'"
        )
        step = _pypa_step(publish)
        step["with"]["repository-url"] = swap(
            str(step["with"]["repository-url"]),
            "https://test.pypi.org/legacy/",
            "https://upload.pypi.org/legacy/",
        )

    assert any("manual run" in p for p in run(swapped_routes))


@pytest.mark.req("ENF-SEC-007")
def test_sdk_dist_mutations_are_caught() -> None:
    def run(mutate: Any) -> list[str]:
        ci = copy.deepcopy(_load("ci.yml"))
        mutate(ci)
        return _dry_run_problems(ci)

    def gone(ci: dict[str, Any]) -> None:
        del ci["jobs"]["sdk-dist"]

    assert run(gone) == ["the sdk-dist dry run is gone"]

    def uploads(ci: dict[str, Any]) -> None:
        ci["jobs"]["sdk-dist"]["steps"].append({"run": "uv publish dist/*"})

    assert any("sends something" in p for p in run(uploads))

    def token(ci: dict[str, Any]) -> None:
        ci["jobs"]["sdk-dist"]["permissions"] = {"id-token": "write", "contents": "read"}

    assert any("OIDC token" in p for p in run(token))

    def no_hostless(ci: dict[str, Any]) -> None:
        ci["jobs"]["sdk-dist"]["steps"] = [
            s for s in ci["jobs"]["sdk-dist"]["steps"] if "find_spec" not in str(s.get("run", ""))
        ]

    assert any("without the host" in p for p in run(no_hostless))

    def no_kit(ci: dict[str, Any]) -> None:
        ci["jobs"]["sdk-dist"]["steps"] = [
            s for s in ci["jobs"]["sdk-dist"]["steps"] if "gabarit" not in str(s.get("run", ""))
        ]

    assert any("example plugins" in p for p in run(no_kit))
