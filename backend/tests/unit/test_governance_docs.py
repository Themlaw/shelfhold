"""Presence and structure checks on the M0 governance, legal and CI documents."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
MILESTONES = ["M0", "M1a", "M1b", "M2", "M3", "M4", "M5", "M6"]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.req("ENF-LEG-010")
def test_license_has_provisional_plugin_exception() -> None:
    text = _read("LICENSE")
    assert "Additional permission (provisional plugin exception)" in text
    assert "PROVISIONAL TEXT" in text
    assert "WILL BE REVIEWED before version 1.0" in text
    sdk_license = _read("backend/src/shelfhold/sdk/LICENSE")
    assert "MIT License" in sdk_license.splitlines()[0]


@pytest.mark.req("ENF-LEG-010", "ENF-SEC-007")
def test_dco_is_enforced_in_ci_and_documented() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/dco.yml"))
    on = workflow.get("on", workflow.get(True))
    assert "pull_request" in on
    runs = [str(s.get("run", "")) for j in workflow["jobs"].values() for s in j["steps"]]
    assert any("Signed-off-by" in r for r in runs)
    contributing = _read("CONTRIBUTING.md")
    assert "DCO 1.1" in contributing
    assert "Signed-off-by:" in contributing
    assert "pas de CLA" in contributing


@pytest.mark.req("ENF-SEC-007")
def test_renovate_pins_digests_with_a_release_age() -> None:
    import json

    config = json.loads(_read("renovate.json"))
    assert "config:best-practices" in config["extends"]
    assert config["pinDigests"] is True
    assert config["minimumReleaseAge"]


@pytest.mark.req("ENF-SEC-007")
def test_ci_lints_workflows_with_actionlint_and_zizmor() -> None:
    runs = _lint_runs()
    assert any("actionlint" in r for r in runs)
    assert any("zizmor" in r for r in runs)


@pytest.mark.req("ENF-LEG-011", "ENF-SEC-011")
def test_cra_and_security_docs_present() -> None:
    cra = _read("docs/legal/cra.md")
    assert "ce n'est pas un avis juridique" in cra
    assert "Critères de bascule" in cra
    security = _read("SECURITY.md")
    assert "signalement privé" in security
    assert "GHSA" in security
    assert "docs/legal/cra.md" in security
    assert "Versions supportées" in security


@pytest.mark.req("ENF-GOV-005")
def test_roadmap_has_time_budget_per_milestone() -> None:
    # The budget box per milestone lives in the spec; the project converts it to agent
    # effort (decision M0 8), tracked per milestone in velocite.md, with the 150 % cut rule.
    spec = _read("docs/spec/03-roadmap-tdd.md")
    for ms in MILESTONES:
        assert re.search(rf"^\| {ms} \|.*\bh \|", spec, re.M), f"no time box for {ms} in 03"
    roadmap = _read("ROADMAP.md")
    for ms in MILESTONES:
        assert re.search(rf"^\| {ms} \|", roadmap, re.M), f"{ms} missing from ROADMAP.md"
    assert "effort agent" in roadmap
    assert "150 %" in roadmap
    velocity = _read("docs/governance/velocite.md")
    for ms in MILESTONES:
        assert re.search(rf"^\| {ms} \|", velocity, re.M), f"{ms} missing from velocite.md"


@pytest.mark.req("ENF-GOV-006")
def test_portes_md_has_p0_p3_stop_and_handover_rules() -> None:
    text = _read("docs/governance/portes.md")
    for gate in ("P0", "P1", "P2", "P3"):
        assert re.search(rf"^\| {gate} \|", text, re.M), gate
    assert "## Règle d'arrêt" in text
    assert "## Règle de passation" in text
    assert "50 %" in text
    assert "releve-depart.md" in text
    assert "reportés" in text  # interviews depend on the maintainer (decision M0 9)


@pytest.mark.req("ENF-DOC-001")
def test_readme_shows_project_status() -> None:
    first_line = _read("README.md").splitlines()[0]
    assert first_line.startswith("**Statut du projet")
    assert "pré-alpha" in first_line
    assert "](AI_POLICY.md)" in _read("README.md")


@pytest.mark.req("ENF-GOV-001")
def test_ai_policy_covers_maintainer_assisted_code() -> None:
    text = _read("AI_POLICY.md")
    assert "majoritairement écrit par des agents" in text
    assert "TDD obligatoire" in text
    assert "Relecture croisée" in text
    assert "Contributeurs extérieurs" in text
    assert "DCO" in text
    governance = _read("GOVERNANCE.md")
    for heading in ("Facteur bus", "Successeur", "Triage et financement", "Archivage propre"):
        assert f"## {heading}" in governance


@pytest.mark.req("ENF-LEG-006")
def test_docs_household_vocabulary_no_sharing_wording() -> None:
    forbidden = re.compile(r"\bpartag\w*", re.IGNORECASE)
    # Published project docs only; spec and ADRs quote the rule itself.
    files = [ROOT / n for n in ("README.md", "GOVERNANCE.md", "ROADMAP.md", "AI_POLICY.md")]
    files += list((ROOT / "docs" / "governance").glob("*.md")) + list(
        (ROOT / "docs" / "legal").glob("*.md")
    )
    allowed = re.compile(
        r"(service de partage|ni un service de partage|pas un service de partage)", re.I
    )
    offenders = []
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line) and not allowed.search(line):
                offenders.append(f"{f.relative_to(ROOT)}:{n}")
    assert not offenders, offenders
    assert "bibliothèque personnelle de votre foyer" in _read("README.md")


@pytest.mark.req("ENF-LEG-004")
def test_denylist_job_exists_and_fails_on_a_test_pattern(tmp_path: Path) -> None:
    ci = yaml.safe_load(_read(".github/workflows/ci.yml"))
    job = ci["jobs"]["denylist"]
    assert any("denylist.sh" in str(s.get("run", "")) for s in job["steps"])
    assert "denylist" in ci["jobs"]["ci-success"]["needs"]
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "doc.md").write_text("contient forbiddenword ici\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(tmp_path)}
    subprocess.run([git, "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run([git, "add", "."], cwd=repo, check=True, env=env)
    script = ROOT / ".github" / "scripts" / "denylist.sh"

    def run(pattern: str | None) -> subprocess.CompletedProcess[str]:
        e = dict(env)
        if pattern is not None:
            e["DENYLIST_NAMES"] = pattern
        return subprocess.run(
            ["bash", str(script)],
            cwd=repo,
            env=e,
            capture_output=True,
            text=True,
            check=False,
        )

    hit = run("forbidden[a-z]+")
    assert hit.returncode == 1
    assert "forbiddenword" not in hit.stdout + hit.stderr  # never prints the match
    assert run("nothing-like-this").returncode == 0
    assert run(None).returncode == 1  # a missing secret fails closed


@pytest.mark.req("ENF-GOV-002", "ENF-GOV-003")
def test_overlap_review_m0_has_sources_and_a_decision() -> None:
    text = _read("docs/governance/chevauchement-M0.md")
    for project in ("Kavita", "Komga", "Suwayomi"):
        assert re.search(rf"\| {project}.*https://github\.com/", text), project
    assert "## Décision" in text
    assert "## Constats" in text
    assert "pre-mortem.md" in text
    assert "velocite.md" in text
    assert re.search(r"20\d\d-\d\d-\d\d", text)


@pytest.mark.req("ENF-GOV-003")
def test_overlap_review_is_an_exit_criterion_of_each_milestone() -> None:
    assert "revue de chevauchement" in _read("ROADMAP.md")
    governance = _read("GOVERNANCE.md")
    assert "## Revue de chevauchement" in governance
    for name in ("Uchiyomi", "Tranga", "Mangarr", "BookOrbit"):
        assert name in governance, name


@pytest.mark.req("ENF-QUAL-001")
def test_coverage_gates_are_configured() -> None:
    pyproject = _read("backend/pyproject.toml")
    assert re.search(r"fail_under\s*=\s*(8\d|9\d)", pyproject)
    assert "branch = true" in pyproject
    ci = _read(".github/workflows/ci.yml")
    assert "diff-cover" in ci
    assert "--fail-under=85" in ci


@pytest.mark.req("ENF-QUAL-002")
def test_import_linter_runs_in_a_ci_step() -> None:
    assert any(re.search(r"\blint-imports\b", r) for r in _lint_runs())


def _lint_runs() -> list[str]:
    ci = yaml.safe_load(_read(".github/workflows/ci.yml"))
    return [str(step["run"]) for step in ci["jobs"]["lint"]["steps"] if "run" in step]


@pytest.mark.req("ENF-PRIV-001")
def test_frontend_sources_load_no_web_font_or_remote_resource() -> None:
    remote = re.compile(
        r"@font-face|@import|url\(\s*['\"]?https?:|\b(?:src|href)\s*=\s*['\"]https?:"
    )
    offenders = []
    for f in (ROOT / "frontend" / "src").rglob("*"):
        if f.suffix not in {".css", ".html", ".svelte", ".json"} or ".test." in f.name:
            continue
        if remote.search(f.read_text(encoding="utf-8")):
            offenders.append(str(f.relative_to(ROOT)))
    assert not offenders, offenders


@pytest.mark.req("ENF-SEC-007")
def test_workflow_actions_are_pinned_by_sha() -> None:
    uses = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.M)
    unpinned = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for ref in uses.findall(wf.read_text(encoding="utf-8")):
            if ref.startswith("./"):
                continue  # reusable workflow of this repository
            if not re.search(r"@[0-9a-f]{40}$", ref):
                unpinned.append(f"{wf.name}: {ref}")
    assert not unpinned, unpinned
