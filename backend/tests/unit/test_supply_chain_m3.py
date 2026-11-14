"""M3 supply-chain tier (L54): cosign on both registries, Scorecard, documentation site, SECURITY.md.

The workflows are read as YAML, never run. The one piece of logic that decides whether the second
registry is used (`.github/scripts/second-registry.sh detect`) is run for real, with and without the
token. ENF-GOV-004 stays open: the second registry works with or without `CODEBERG_TOKEN`, and the
image is signed on GHCR in every case.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPT = ROOT / ".github" / "scripts" / "second-registry.sh"
PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
PUSH_MAIN = re.compile(r"github\.event_name == 'push'.*github\.ref == 'refs/heads/main'")
ENABLED = "steps.second.outputs.enabled == 'true'"
BASH = shutil.which("bash") or "/bin/bash"
NEW_WORKFLOWS = ("scorecard.yml", "docs.yml")


def _load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    triggers = workflow.get(True) or workflow.get("on") or {}
    return triggers if isinstance(triggers, dict) else {k: {} for k in triggers}


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# --- SECURITY.md and cra.md: deadlines, supported versions, advisories, no compliance claim ---

CLAIM = re.compile(r"conforme|conformité|certifi|compliant|compliance|certified", re.I)
DENIAL = re.compile(r"ne prétend|aucune|sans prétendre|pas de |n'affirme|ne garantit|pas un avis", re.I)


def _security_problems(text: str) -> list[str]:
    problems: list[str] = []
    if not re.search(r"accusé de réception[^\n]*(?:≤|au plus|sous)\s*7 jours", text):
        problems.append("acknowledgement deadline of 7 days missing")
    if not re.search(r"correctif[^\n]*(?:≤|au plus|sous)\s*90 jours[^\n]*au mieux", text):
        problems.append("fix target of 90 days, best effort, missing")
    if not (
        "X.Y" in text
        and re.search(r"courante", text)
        and re.search(r"précédente", text)
    ):
        problems.append("supported versions X.Y current and previous missing")
    if not re.search(r"avis[^\n]*publi", text, re.I):
        problems.append("published advisories missing")
    if re.search(r"à venir avant 1\.0|seront publiés avant", text):
        problems.append("deadlines still announced as future")
    for line in text.splitlines():
        if CLAIM.search(line) and not DENIAL.search(line):
            problems.append(f"possible compliance claim: {line.strip()}")
    return problems


@pytest.mark.req("ENF-SEC-011", "ENF-LEG-011")
def test_security_md_has_deadlines_and_supported_versions() -> None:
    text = _read("SECURITY.md")
    assert _security_problems(text) == []
    assert "docs/legal/cra.md" in text
    assert "GHSA" in text and "ne prétend" in text
    # the way to check what is installed is documented and linked from the policy
    assert "verifier-les-images" in text


@pytest.mark.req("ENF-SEC-011", "ENF-LEG-011")
def test_security_md_checker_rejects_weakened_or_overclaiming_text() -> None:
    good = _read("SECURITY.md")
    weaker = good.replace("7 jours", "30 jours")
    assert any("7 days" in p for p in _security_problems(weaker))
    no_best_effort = good.replace("au mieux", "garanti")
    assert any("90 days" in p for p in _security_problems(no_best_effort))
    no_versions = good.replace("précédente", "ancienne")
    assert any("supported versions" in p for p in _security_problems(no_versions))
    future = good + "\nLes délais seront publiés avant 1.0.\n"
    assert any("future" in p for p in _security_problems(future))
    overclaim = good + "\nLe projet est conforme au Cyber Resilience Act.\n"
    assert any("compliance claim" in p for p in _security_problems(overclaim))


@pytest.mark.req("ENF-LEG-011")
def test_cra_md_same_spirit_and_covers_signatures() -> None:
    text = _read("docs/legal/cra.md")
    assert _security_problems("accusé de réception ≤ 7 jours\n" + text) is not None
    for line in text.splitlines():
        assert not (CLAIM.search(line) and not DENIAL.search(line)), line
    assert "ne prétend" in text or "n'affirme aucune conformité" in text
    assert "cosign" in text and "verifier-les-images" in text
    assert "Scorecard" in text
    assert "SECURITY.md" in text
    assert "7 jours" in text and "90 jours" in text
    assert "X.Y" in text


# --- cosign on both registries, with or without the second registry's token ---


def _detect(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str]:
    out, summary = tmp_path / "out", tmp_path / "summary"
    out.write_text("")
    summary.write_text("")
    full = {
        "PATH": os.environ["PATH"],
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(summary),
        "OWNER": "SomeOwner",
        "REPO_NAME": "Shelfhold",
        **env,
    }
    done = subprocess.run(  # noqa: S603
        [BASH, str(SCRIPT), "detect"], env=full, capture_output=True, text=True, cwd=tmp_path
    )
    pairs = dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    return done, pairs, summary.read_text(encoding="utf-8")


@pytest.mark.req("ENF-SEC-007")
def test_second_registry_absent_token_skips_without_failing_and_says_so(tmp_path: Path) -> None:
    done, outputs, summary = _detect(tmp_path, HAS_TOKEN="false", SECOND_REGISTRY="")
    assert done.returncode == 0, done.stderr
    assert outputs == {"enabled": "false"}
    assert "CODEBERG_TOKEN" in summary and "GHCR" in summary and "ENF-GOV-004" in summary
    assert re.search(r"skipped|ignor|absent", summary, re.I)


@pytest.mark.req("ENF-SEC-007")
def test_second_registry_with_token_defaults_to_codeberg_owner(tmp_path: Path) -> None:
    done, outputs, summary = _detect(tmp_path, HAS_TOKEN="true", SECOND_REGISTRY="")
    assert done.returncode == 0, done.stderr
    assert outputs["enabled"] == "true"
    assert outputs["image"] == "codeberg.org/someowner/shelfhold"  # lowercase, `codeberg.org/<owner>`
    assert outputs["host"] == "codeberg.org"
    assert "codeberg.org/someowner/shelfhold" in summary


@pytest.mark.req("ENF-SEC-007")
def test_second_registry_variable_overrides_the_default(tmp_path: Path) -> None:
    done, outputs, _ = _detect(tmp_path, HAS_TOKEN="true", SECOND_REGISTRY="registry.example.org:5000/team")
    assert done.returncode == 0, done.stderr
    assert outputs["image"] == "registry.example.org:5000/team/shelfhold"
    assert outputs["host"] == "registry.example.org:5000"


@pytest.mark.req("ENF-SEC-007")
@pytest.mark.parametrize(
    "hostile",
    [
        "evil.example/x;rm -rf /",
        "evil.example/$(id)",
        "evil.example/a\nenabled=true",
        "https://evil.example/x",
        "evil.example/../x",
        "evil example/x",
        "evil.example/`id`",
        "/leading-slash",
    ],
)
def test_second_registry_hostile_variable_never_reaches_the_outputs(tmp_path: Path, hostile: str) -> None:
    done, outputs, _ = _detect(tmp_path, HAS_TOKEN="true", SECOND_REGISTRY=hostile)
    assert done.returncode != 0  # a configuration mistake is loud when the token is there
    assert "image" not in outputs and outputs.get("enabled") != "true"
    # and with no token the odd value is ignored: nothing to publish, nothing emitted
    quiet, quiet_outputs, _ = _detect(tmp_path, HAS_TOKEN="false", SECOND_REGISTRY=hostile)
    assert quiet.returncode == 0 and quiet_outputs == {"enabled": "false"}


@pytest.mark.req("ENF-SEC-007")
def test_second_registry_script_never_sees_or_prints_the_token(tmp_path: Path) -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "CODEBERG_TOKEN" in text  # named in the message to the maintainer, never read
    assert not re.search(r"\$\{?(CODEBERG_TOKEN|TOKEN|PASSWORD)\b", text)
    done, _, summary = _detect(tmp_path, HAS_TOKEN="true", CODEBERG_TOKEN="s3cr3t-value-123")
    assert "s3cr3t-value-123" not in done.stdout + done.stderr + summary


def _step_text(step: dict[str, Any]) -> str:
    return yaml.safe_dump(step)


def _second_problems(workflows: dict[str, dict[str, Any]]) -> list[str]:
    """Every way the second registry could be reached without the token being checked first,
    or the token could reach a step that must not hold it."""
    problems: list[str] = []
    publish = workflows["image-publish.yml"]
    on_call = _on(publish)["workflow_call"]
    secret = (on_call.get("secrets") or {}).get("CODEBERG_TOKEN")
    if not secret or secret.get("required") is not False:
        problems.append("CODEBERG_TOKEN must be an optional workflow_call secret")
    steps = publish["jobs"]["merge"]["steps"]
    detect = [s for s in steps if s.get("id") == "second"]
    if len(detect) != 1 or "second-registry.sh" not in str(detect[0].get("run", "")):
        problems.append("one detect step `second` running second-registry.sh required")
    else:
        text = _step_text(detect[0])
        if "secrets.CODEBERG_TOKEN" in text and "secrets.CODEBERG_TOKEN != ''" not in text:
            problems.append("the detect step must not receive the token value")
        if re.search(r"\bexit 1\b", str(detect[0].get("run", ""))):
            problems.append("the detect step must not fail when the token is absent")
        if detect[0].get("if"):
            problems.append("the detect step runs every time")
    seen_detect = False
    for step in steps:
        if step.get("id") == "second":
            seen_detect = True
            continue
        text = _step_text(step)
        touches = "secrets.CODEBERG_TOKEN" in text or "SECOND_IMAGE" in text or "steps.second." in text
        if touches and ENABLED not in str(step.get("if", "")):
            problems.append(f"step not conditioned on the token: {step.get('name')}")
        if touches and not seen_detect:
            problems.append(f"step before detection: {step.get('name')}")
    # the token is read by the login step only (never by a run script, never on a command line)
    holders = [s for s in steps if "secrets.CODEBERG_TOKEN" in _step_text(s) and s.get("id") != "second"]
    if [str(s.get("uses", "")).split("@")[0] for s in holders] != ["docker/login-action"]:
        problems.append("the token must reach docker/login-action only")
    return problems


def _both() -> dict[str, dict[str, Any]]:
    return {n: _load(n) for n in ("image-publish.yml", "ci.yml", "release.yml")}


@pytest.mark.req("ENF-SEC-007")
def test_release_workflow_signs_both_registries() -> None:
    workflows = _both()
    assert _second_problems(workflows) == []
    steps = workflows["image-publish.yml"]["jobs"]["merge"]["steps"]
    names = [str(s.get("name", "")) for s in steps]
    # cosign is installed from a pinned action; GHCR is always signed, with no condition
    install = [s for s in steps if str(s.get("uses", "")).startswith("sigstore/cosign-installer@")]
    assert len(install) == 1 and PIN.match(install[0]["uses"].split(" ")[0])
    assert "if" not in install[0]
    sign = [s for s in steps if "cosign sign" in str(s.get("run", ""))]
    always = [s for s in sign if not s.get("if")]
    gated = [s for s in sign if ENABLED in str(s.get("if", ""))]
    assert len(always) == 1 and len(gated) == 1
    # keyless: OIDC only, no key material anywhere, the manifest list and both platform images
    for s in sign:
        run = str(s["run"])
        assert "--key" not in run and "COSIGN_PRIVATE_KEY" not in run and "COSIGN_PASSWORD" not in run
        for digest in ("index", "platform1", "platform2"):
            assert digest in str(s.get("env", {})) + run
    assert 'IMAGE}@' in str(always[0]["run"]) or "$IMAGE@" in str(always[0]["run"])
    assert "SECOND_IMAGE" in str(gated[0]["run"]) + str(gated[0].get("env", {}))
    # what was signed is verified in the same run, on both registries
    verify = [s for s in steps if "cosign verify" in str(s.get("run", ""))]
    assert verify, names
    joined = "\n".join(str(s["run"]) for s in verify)
    assert "--certificate-identity" in joined and "--certificate-oidc-issuer" in joined
    assert "token.actions.githubusercontent.com" in joined
    assert "SECOND_IMAGE" in joined and "IMAGE" in joined
    # the caller hands over the optional secret by name, never `secrets: inherit`
    caller = workflows["ci.yml"]["jobs"]["image-publish"]
    assert caller["secrets"] == {"CODEBERG_TOKEN": "${{ secrets.CODEBERG_TOKEN }}"}
    assert PUSH_MAIN.search(str(caller["if"]))
    assert "inherit" not in yaml.safe_dump(workflows["ci.yml"]["jobs"])
    # the name of the second registry is a variable with a documented default, never a literal
    text = (WORKFLOWS / "image-publish.yml").read_text(encoding="utf-8")
    assert "vars.SECOND_REGISTRY" in text
    assert not re.search(r"codeberg\.org/(?!<owner>)[\w.-]+", text)
    assert "codeberg.org/<owner>" in _read("docs/admin/verifier-les-images.md")


@pytest.mark.req("ENF-SEC-007")
def test_second_registry_mutations_are_caught() -> None:
    def run(mutate: Any) -> list[str]:
        workflows = copy.deepcopy(_both())
        mutate(workflows)
        return _second_problems(workflows)

    def drop_condition(w: dict[str, dict[str, Any]]) -> None:
        for step in w["image-publish.yml"]["jobs"]["merge"]["steps"]:
            if "SECOND_IMAGE" in _step_text(step) and "cosign sign" in str(step.get("run", "")):
                step.pop("if", None)

    assert any("not conditioned" in p for p in run(drop_condition))

    def required(w: dict[str, dict[str, Any]]) -> None:
        w["image-publish.yml"][True]["workflow_call"]["secrets"]["CODEBERG_TOKEN"]["required"] = True

    assert any("optional" in p for p in run(required))

    def leak_to_script(w: dict[str, dict[str, Any]]) -> None:
        for step in w["image-publish.yml"]["jobs"]["merge"]["steps"]:
            if "cosign sign" in str(step.get("run", "")) and step.get("if"):
                step.setdefault("env", {})["T"] = "${{ secrets.CODEBERG_TOKEN }}"

    assert any("login-action only" in p for p in run(leak_to_script))

    def failing_detect(w: dict[str, dict[str, Any]]) -> None:
        for step in w["image-publish.yml"]["jobs"]["merge"]["steps"]:
            if step.get("id") == "second":
                step["run"] += "\n[ x ] || exit 1"

    assert any("must not fail" in p for p in run(failing_detect))


@pytest.mark.req("ENF-SEC-007")
def test_the_second_registry_token_never_reaches_a_pull_request_run() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = _load(path.name)
        triggers = _on(workflow)
        assert "pull_request_target" not in triggers, path.name
        for job_id, job in workflow["jobs"].items():
            text = yaml.safe_dump(job)
            if "CODEBERG_TOKEN" not in text:
                continue
            assert "pull_request" not in str(job.get("if", "")), (path.name, job_id)
            if "pull_request" in triggers:
                assert PUSH_MAIN.search(str(job.get("if", ""))), (path.name, job_id)
    # no literal credential in any workflow or script of this lot
    for path in [*WORKFLOWS.glob("*.yml"), *SCRIPT.parent.glob("*.sh")]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"ghp_|glpat-|-----BEGIN|password:\s*[^$\s]", text), path.name
        assert "--password " not in text and "echo \"$CODEBERG" not in text, path.name


@pytest.mark.req("ENF-SEC-007")
def test_release_tags_reach_the_second_registry_only_when_signed_and_token_present() -> None:
    release = _load("release.yml")
    job = release["jobs"]["second-registry"]
    assert job["needs"] == "image-tags" or "image-tags" in job["needs"]
    assert job["permissions"] == {"contents": "read", "packages": "read"}  # no id-token here
    steps = job["steps"]
    assert any(s.get("id") == "second" and "second-registry.sh" in str(s.get("run")) for s in steps)
    for step in steps:
        text = _step_text(step)
        if "SECOND_IMAGE" in text or "secrets.CODEBERG_TOKEN" in text:
            assert ENABLED in str(step.get("if", "")), step.get("name")
    texts = [str(s.get("run", "")) for s in steps]
    verify = next(i for i, t in enumerate(texts) if "cosign verify" in t)
    tag = next(i for i, t in enumerate(texts) if "imagetools create" in t)
    assert verify < tag  # an unsigned digest is never given a version tag
    assert "exit 1" not in texts[verify] or "::error::" in texts[verify]
    # the tag is validated before it reaches a command
    flat = "\n".join(texts)
    assert re.search(r"\^v\(\[0-9\]\+\)", flat)


@pytest.mark.req("ENF-SEC-007")
def test_new_and_changed_jobs_have_minimal_permissions() -> None:
    expected = {
        ("scorecard.yml", "analysis"): {"contents": "read", "security-events": "write"},
        ("docs.yml", "build"): {"contents": "read"},
        ("docs.yml", "deploy"): {"pages": "write", "id-token": "write"},
        ("release.yml", "second-registry"): {"contents": "read", "packages": "read"},
    }
    for (name, job_id), perms in expected.items():
        workflow = _load(name)
        assert workflow.get("permissions") == {}, name  # nothing by default
        assert workflow["jobs"][job_id]["permissions"] == perms, (name, job_id)
    for name in NEW_WORKFLOWS:
        workflow = _load(name)
        assert "pull_request_target" not in _on(workflow)
        for job_id, job in workflow["jobs"].items():
            assert isinstance(job.get("permissions"), dict), (name, job_id)
            assert job.get("timeout-minutes"), (name, job_id)
            for step in job["steps"]:
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False, (name, job_id)


# --- OpenSSF Scorecard ---


@pytest.mark.req("ENF-SEC-007")
def test_scorecard_workflow_pinned() -> None:
    workflow = _load("scorecard.yml")
    triggers = _on(workflow)
    assert "schedule" in triggers and "pull_request" not in triggers
    cron = triggers["schedule"][0]["cron"]
    assert len(cron.split()) == 5 and cron.split()[4] != "*"  # weekly, one day of the week
    job = workflow["jobs"]["analysis"]
    refs = [str(s["uses"]) for s in job["steps"] if "uses" in s]
    assert any(r.startswith("ossf/scorecard-action@") for r in refs)
    assert any(r.startswith("github/codeql-action/upload-sarif@") for r in refs)
    assert refs and all(PIN.match(r.split(" ")[0]) for r in refs), refs
    action = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("ossf/"))
    assert action["with"]["results_format"] == "sarif"
    # nothing is published outside the repository, so the job holds no OIDC token
    assert action["with"]["publish_results"] is False
    assert "id-token" not in job["permissions"]
    assert job.get("if") and "github.repository" in str(job["if"])  # never on a fork


# --- documentation site (D17: Zensical, built from docs/) ---


@pytest.mark.req("ENF-DOC-001")
def test_docs_workflow_builds_on_pull_requests_and_publishes_on_release() -> None:
    workflow = _load("docs.yml")
    triggers = _on(workflow)
    assert "pull_request" in triggers and "release" in triggers
    assert triggers["release"]["types"] == ["published"]
    assert "docs/**" in triggers["pull_request"]["paths"]
    build, deploy = workflow["jobs"]["build"], workflow["jobs"]["deploy"]
    assert deploy["needs"] == "build"
    assert deploy["if"] == "github.event_name == 'release'"
    assert deploy["environment"]["name"] == "github-pages"
    refs = [str(s["uses"]) for j in (build, deploy) for s in j["steps"] if "uses" in s]
    assert all(PIN.match(r.split(" ")[0]) for r in refs), refs
    assert any(r.startswith("actions/deploy-pages@") for r in refs)
    assert any(r.startswith("actions/upload-pages-artifact@") for r in refs)
    text = "\n".join(str(s.get("run", "")) for s in build["steps"])
    assert "zensical build" in text and "--strict" in text
    # the code of a release is the TAG's, and the tag is validated before it is used
    assert re.search(r"\^v\(\[0-9\]\+\)", text)
    assert "refs/tags/" in yaml.safe_dump(build["steps"])
    # the site is built from locked tools: no unpinned download
    assert "pip install" not in text and "@latest" not in text


@pytest.mark.req("ENF-DOC-001")
def test_docs_site_config_is_zensical_over_docs() -> None:
    config = yaml.safe_load(_read("docs/mkdocs.yml"))
    assert config["site_name"] == "Shelfhold"
    assert config["docs_dir"] == "."
    assert config["site_dir"].startswith("_")  # inside the project root, ignored by git
    assert "site_url" not in config or "divprotocol" not in str(config["site_url"])  # no own domain
    assert "_site" in _read(".gitignore")
    lock = _read("backend/uv.lock")
    assert 'name = "zensical"' in lock
    dev = tomllib.loads(_read("backend/pyproject.toml"))["dependency-groups"]["dev"]
    assert any(d.startswith("zensical==") for d in dev)  # pinned exactly


def _zensical() -> str:
    beside = Path(sys.executable).parent / "zensical"
    if beside.is_file():
        return str(beside)
    found = shutil.which("zensical")
    if found:
        return found
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail("`zensical` is required in CI and is not installed")
    pytest.skip("`zensical` is not installed on this machine (CI has it and runs this test)")


@pytest.mark.req("ENF-DOC-001")
def test_docs_site_builds_without_warning(tmp_path: Path) -> None:
    """Offline: the site is built from a copy of docs/, in strict mode (any warning aborts)."""
    exe = _zensical()
    project = tmp_path / "docs"
    shutil.copytree(ROOT / "docs", project, ignore=shutil.ignore_patterns("_site", ".cache"))
    done = subprocess.run(  # noqa: S603
        [exe, "build", "--strict", "-f", str(project / "mkdocs.yml")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "NO_COLOR": "1"},
        timeout=240,
    )
    output = done.stdout + done.stderr
    assert done.returncode == 0, output[-3000:]
    assert not re.search(r"warning|issues? found", output, re.I), output[-3000:]
    assert (project / "_site" / "index.html").is_file()


@pytest.mark.req("ENF-DOC-001")
def test_docs_site_build_rejects_a_broken_link(tmp_path: Path) -> None:
    exe = _zensical()
    project = tmp_path / "docs"
    shutil.copytree(ROOT / "docs", project, ignore=shutil.ignore_patterns("_site", ".cache"))
    (project / "index.md").write_text("# Home\n\n[gone](does-not-exist.md)\n", encoding="utf-8")
    done = subprocess.run(  # noqa: S603
        [exe, "build", "--strict", "-f", str(project / "mkdocs.yml")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "NO_COLOR": "1"},
        timeout=240,
    )
    assert done.returncode != 0


# --- the verification is documented for both registries; ENF-GOV-004 stays open ---


@pytest.mark.req("ENF-SEC-007")
def test_cosign_verification_documented_for_both_registries() -> None:
    text = _read("docs/admin/verifier-les-images.md")
    assert text.count("cosign verify") >= 2
    assert "ghcr.io/themlaw/shelfhold" in text
    assert "codeberg.org/<owner>/shelfhold" in text
    assert "--certificate-identity-regexp" in text and "image-publish" in text
    assert "https://token.actions.githubusercontent.com" in text
    assert "gh attestation verify" in text
    for needle in ("CODEBERG_TOKEN", "SECOND_REGISTRY", "sans le jeton"):
        assert needle in text, needle
    # the release page and the pipeline page point to it, and the false statement is gone
    assert "verifier-les-images" in _read("docs/releasing.md")
    ci_image = _read("docs/ci-image.md")
    assert "verifier-les-images" in ci_image and "Pas de cosign avant M3" not in ci_image
    # no real organisation name: the second registry is a placeholder
    assert not re.search(r"codeberg\.org/(?!<owner>)[\w.-]+", text)


@pytest.mark.req("ENF-SEC-007")
def test_enf_gov_004_stays_open_and_is_not_claimed_delivered() -> None:
    exceptions = tomllib.loads(_read("tools/traceability-exceptions.toml"))
    deferred = [d for d in exceptions["deferred"] if d["requirement"] == "ENF-GOV-004"]
    assert len(deferred) == 1
    decision = _read("docs/decisions/M3.md")
    section = decision.split("## L54", 1)[1].split("\n## ", 1)[0]
    assert re.search(r"ENF-GOV-004[^\n]*(reste ouverte|n'est pas livrée|reste `\[\[deferred\]\]`)", section)
    # no test of this lot claims the requirement as its own delivery (the tests above only
    # exercise the branches); the traceability tool sees the deferral
    for line in _read("CHANGELOG.md").splitlines():
        assert not ("ENF-GOV-004" in line and re.search(r"livrée|delivered|done", line, re.I)), line
