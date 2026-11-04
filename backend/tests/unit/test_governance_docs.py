"""Presence and structure checks on the M0 governance, legal and CI documents."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
MILESTONES = ["M0", "M1a", "M1b", "M2", "M3", "M4", "M5", "M6"]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.req("ENF-LEG-010")
def test_license_has_provisional_plugin_exception() -> None:
    text = _read("LICENSE")
    assert "Additional permission (provisional plugin exception)" in text
    sdk_license = _read("backend/src/shelfhold/sdk/LICENSE")
    assert "MIT License" in sdk_license.splitlines()[0]


@pytest.mark.req("ENF-LEG-010", "ENF-SEC-007")
def test_commits_signed_off() -> None:
    job = yaml.safe_load(_read(".github/workflows/ci.yml"))["jobs"]["dco"]
    assert job["if"] == "github.event_name == 'pull_request'"
    assert any("Signed-off-by" in str(s.get("run", "")) for s in job["steps"])
    assert "pull_request.user.login" in str(job["steps"])  # PR author, not a forgeable git name
    contributing = _read("CONTRIBUTING.md")
    assert "Signed-off-by:" in contributing
    assert "pas de CLA" in contributing


@pytest.mark.req("ENF-LEG-011", "ENF-SEC-011")
def test_cra_and_security_docs_present() -> None:
    assert (ROOT / "docs" / "legal" / "cra.md").is_file()
    assert "docs/legal/cra.md" in _read("SECURITY.md")


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
    assert (ROOT / "docs" / "recherche" / "releve-depart.md").is_file()


@pytest.mark.req("ENF-DOC-001")
def test_readme_shows_project_status() -> None:
    first_line = _read("README.md").splitlines()[0]
    assert first_line.startswith("**Statut du projet")
    assert "pré-alpha" in first_line


@pytest.mark.req("ENF-GOV-001")
def test_ai_policy_covers_maintainer_assisted_code() -> None:
    assert "DCO" in _read("AI_POLICY.md")
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


@pytest.mark.req("ENF-LEG-004")
def test_denylist_names_absent_from_docs_and_templates(tmp_path: Path) -> None:
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


@pytest.mark.req("ENF-QUAL-001")
def test_coverage_gates_are_configured() -> None:
    pyproject = _read("backend/pyproject.toml")
    assert re.search(r"fail_under\s*=\s*(8\d|9\d)", pyproject)
    assert "branch = true" in pyproject


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
