"""Supply chain of the release workflows (ENF-SEC-007 palier M1b, ENF-QUAL-007).

The workflows are read as YAML, never run: provenance and SBOM attestations on the pushed digest,
release proofs attached as assets, Grype and CodeQL, and a GHCR cleanup that can only touch the
`pr-*` tags. Every action is pinned by SHA and the write scopes of the OIDC token stay on one job.
"""

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.support.tools import require_tool

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def _uses(job: dict[str, Any]) -> list[str]:
    return [str(s["uses"]) for s in job.get("steps", []) if "uses" in s]


def _text(job: dict[str, Any]) -> str:
    return "\n".join(str(s.get("run", "")) for s in job.get("steps", []))


@pytest.mark.req("ENF-SEC-007", "ENF-QUAL-007")
def test_release_assets_include_sbom_and_attestations() -> None:
    """Original name: test_release_assets_include_sbom_and_attestations."""
    publish = _load("image-publish.yml")["jobs"]["merge"]
    uses = "\n".join(_uses(publish))
    assert "actions/attest-build-provenance@" in uses
    assert "actions/attest-sbom@" in uses
    # the attestations are made on the digest of the manifest and on each platform digest
    steps = "\n".join(str(s) for s in publish["steps"])
    assert "subject-digest" in steps and "spdx-json" in steps

    proofs = _load("release.yml")["jobs"]["release-proofs"]
    assert proofs["permissions"]["contents"] == "write"
    text = _text(proofs) + "\n" + "\n".join(str(s) for s in proofs["steps"])
    for asset in ("sbom.spdx.json", "provenance.intoto.jsonl", "test-report", "bench-reference"):
        assert asset in text, asset
    assert "gh release upload" in text and "--clobber" in text
    script = (ROOT / ".github" / "scripts" / "release-proofs.sh").read_text(encoding="utf-8")
    assert "release-proofs.sh" in text
    # what is attached is what was verified: signer, ref and commit pinned, statements read from
    # the output of the verify itself (no separate download)
    assert "gh attestation verify" in script and "gh attestation download" not in script
    assert "--signer-workflow" in script and "image-publish.yml" in script
    assert "--source-ref refs/heads/main" in script and '--source-digest "$SHA"' in script
    assert ".[].verificationResult.statement" in script and "--format json" in script
    # the benchmark reference is versioned in the repository (artifacts expire)
    assert (ROOT / ".github" / "bench-reference.json").is_file()
    # the proofs wait for the image of the release
    assert "image-tags" in proofs["needs"]


@pytest.mark.req("ENF-QUAL-007")
def test_ghcr_cleanup_policy_only_pr_tags() -> None:
    """Original name: test_ghcr_cleanup_policy_only_pr_tags."""
    workflow = _load("cleanup.yml")
    assert "pull_request" not in workflow[True]  # `on` is parsed as True by YAML 1.1
    assert workflow.get("permissions") == {}
    (job,) = workflow["jobs"].values()
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    text = _text(job)
    # only tags that start with pr-, and never an untagged version (platform manifests,
    # signatures, attestations)
    assert "pr-" in text
    assert "untagged" not in text.replace("never untagged", "")
    assert not re.search(r"delete-package-versions|delete-only-untagged", "\n".join(_uses(job)))
    assert re.search(r"startswith\(\"pr-\"\)|\^pr-", text)
    assert "latest" not in text and "edge" not in text.replace("pr-", "")


CONTENTS_WRITE_ALLOWED = {("release.yml", "release-proofs")}
OIDC_HOLDER = ("image-publish.yml", "merge")
PUSH_MAIN = re.compile(r"github\.event_name == 'push'.*github\.ref == 'refs/heads/main'")


def _all() -> dict[str, dict[str, Any]]:
    return {p.name: _load(p.name) for p in sorted(WORKFLOWS.glob("*.yml"))}


def _scopes(perms: Any) -> dict[str, str]:
    return perms if isinstance(perms, dict) else {}


def _leaks(workflows: dict[str, dict[str, Any]]) -> list[str]:
    """Every way a write scope, or the OIDC token, could reach a job it should not."""
    leaks: list[str] = []
    for name, workflow in workflows.items():
        if "pull_request_target" in (workflow.get(True) or workflow.get("on") or {}):
            leaks.append(f"{name}: pull_request_target")
        tops = [("workflow", workflow.get("permissions"))]
        tops += [(job_id, job.get("permissions")) for job_id, job in workflow["jobs"].items()]
        for job_id, perms in tops:
            where = (name, job_id)
            if perms == "write-all":
                leaks.append(f"{name}/{job_id}: write-all")
            scopes = _scopes(perms)
            if scopes.get("contents") == "write" and where not in CONTENTS_WRITE_ALLOWED:
                leaks.append(f"{name}/{job_id}: contents: write")
            if "id-token" in scopes or "attestations" in scopes:
                job = workflow["jobs"].get(job_id, {})
                if job_id == "workflow":
                    leaks.append(f"{name}: id-token or attestations at workflow level")
                elif "uses" in job:  # a caller: only on a push to main, never on a pull request
                    if not PUSH_MAIN.search(str(job.get("if", ""))):
                        leaks.append(f"{name}/{job_id}: caller of OIDC not limited to main")
                elif where != OIDC_HOLDER:
                    leaks.append(f"{name}/{job_id}: id-token or attestations")
    return leaks


def _mutated(mutate: Any) -> list[str]:
    workflows = copy.deepcopy(_all())
    mutate(workflows)
    return _leaks(workflows)


@pytest.mark.req("ENF-SEC-007")
def test_attestation_permissions_scoped_to_publish_job() -> None:
    """Original name: test_attestation_permissions_scoped_to_publish_job."""
    assert _leaks(_all()) == []
    publish = _load("image-publish.yml")["jobs"]["merge"]["permissions"]
    assert publish == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    ci = _load("ci.yml")["jobs"]["image-publish"]
    assert ci["permissions"]["id-token"] == "write" and ci["permissions"]["attestations"] == "write"


@pytest.mark.req("ENF-SEC-007")
def test_mutation_oidc_scope_on_a_pull_request_caller_is_a_leak() -> None:
    def mutate(w: dict[str, dict[str, Any]]) -> None:
        w["ci.yml"]["jobs"]["image-pr"]["permissions"]["id-token"] = "write"

    assert any("image-pr" in leak for leak in _mutated(mutate))

    def attest(w: dict[str, dict[str, Any]]) -> None:
        w["ci.yml"]["jobs"]["image-pr"]["permissions"]["attestations"] = "write"

    assert any("image-pr" in leak for leak in _mutated(attest))


@pytest.mark.req("ENF-SEC-007")
def test_mutation_write_all_is_a_leak() -> None:
    def job(w: dict[str, dict[str, Any]]) -> None:
        w["image-publish.yml"]["jobs"]["publish"]["permissions"] = "write-all"

    assert any("write-all" in leak for leak in _mutated(job))

    def top(w: dict[str, dict[str, Any]]) -> None:
        w["ci.yml"]["permissions"] = "write-all"

    assert any("write-all" in leak for leak in _mutated(top))


@pytest.mark.req("ENF-SEC-007")
def test_mutation_contents_write_outside_the_allow_list_is_a_leak() -> None:
    def mutate(w: dict[str, dict[str, Any]]) -> None:
        w["release.yml"]["jobs"]["test-report"]["permissions"]["contents"] = "write"

    assert any("test-report" in leak for leak in _mutated(mutate))
    assert {("release.yml", "release-proofs")} == CONTENTS_WRITE_ALLOWED


@pytest.mark.req("ENF-SEC-007")
def test_mutation_pull_request_target_is_a_leak() -> None:
    def mutate(w: dict[str, dict[str, Any]]) -> None:
        w["ci.yml"][True]["pull_request_target"] = {}

    assert any("pull_request_target" in leak for leak in _mutated(mutate))


@pytest.mark.req("ENF-SEC-007")
def test_all_actions_pinned_by_sha_including_new_workflows() -> None:
    seen = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in workflow["jobs"].values():
            for ref in _uses(job):
                seen += 1
                assert PIN.match(ref), f"{path.name}: {ref}"
    assert seen > 20


@pytest.mark.req("ENF-SEC-007")
def test_grype_scan_job_uploads_sarif_without_blocking_on_findings() -> None:
    ci = _load("ci.yml")["jobs"]
    scan = ci["scan"]
    uses = "\n".join(_uses(scan))
    assert "anchore/scan-action@" in uses
    assert "github/codeql-action/upload-sarif@" in uses
    assert scan["permissions"] == {"contents": "read", "security-events": "write"}
    text = "\n".join(str(s) for s in scan["steps"])
    assert "sarif" in text and "fail-build" in text
    # a vulnerability published tomorrow must not break an unrelated PR: reported, not required
    gate = "\n".join(str(s.get("run", "")) for s in ci["ci-success"]["steps"])
    assert "SCAN" in gate and 'check_filtered "$SCAN"' not in gate


@pytest.mark.req("ENF-SEC-007")
def test_codeql_covers_python_and_typescript() -> None:
    workflow = _load("codeql.yml")
    assert workflow.get("permissions") == {}
    job = workflow["jobs"]["analyze"]
    assert job["permissions"] == {"contents": "read", "security-events": "write"}
    languages = {entry["language"] for entry in job["strategy"]["matrix"]["include"]}
    assert languages == {"python", "javascript-typescript"}
    assert "pull_request" in workflow[True] and "schedule" in workflow[True]


# --- release tool downloads: cache keyed by version and sha256, verified after every restore ---

TOOLS = ("hadolint", "gitleaks")


def _lint_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _load("ci.yml")["jobs"]["lint"]["steps"]
    return steps


@pytest.mark.req("ENF-QUAL-007")
@pytest.mark.parametrize("tool", TOOLS)
def test_tool_download_is_cached_by_version_and_sha256(tool: str) -> None:
    steps = _lint_steps()
    caches = [s for s in steps if str(s.get("uses", "")).startswith("actions/cache@")]
    (cache,) = [s for s in caches if tool in str(s["with"]["key"])]
    assert PIN.match(cache["uses"].split(" ")[0])
    key = str(cache["with"]["key"])
    assert f"{tool.upper()}_VERSION" in key and f"{tool.upper()}_SHA256" in key
    # the fetch step comes after the restore and always checks the sum, hit or miss
    runs = [str(s.get("run", "")) for s in steps]
    fetch = [i for i, r in enumerate(runs) if "fetch-tool.sh" in r and tool in r]
    assert fetch and steps.index(cache) < fetch[0]
    script = (ROOT / ".github" / "scripts" / "fetch-tool.sh").read_text(encoding="utf-8")
    assert "sha256sum --check --strict" in script
    assert script.rindex("sha256sum --check --strict") > script.index("curl -")  # after the fetch


@pytest.mark.req("ENF-QUAL-007")
def test_fetch_tool_rejects_a_poisoned_cache(tmp_path: Path) -> None:
    import hashlib
    import shutil
    import subprocess

    BASH = shutil.which("bash") or "/bin/bash"

    script = ROOT / ".github" / "scripts" / "fetch-tool.sh"
    good = b"tool"
    digest = hashlib.sha256(good).hexdigest()
    dest = tmp_path / "tool"
    dest.write_bytes(b"poisoned")
    src = tmp_path / "src"
    src.write_bytes(good)
    ok = subprocess.run(  # noqa: S603
        [BASH, str(script), src.as_uri(), digest, str(dest)], capture_output=True, text=True
    )
    assert ok.returncode == 0 and dest.read_bytes() == good  # bad cache replaced by a fresh fetch
    dest.write_bytes(b"poisoned")
    bad = subprocess.run(  # noqa: S603
        [BASH, str(script), src.as_uri(), "0" * 64, str(dest)], capture_output=True, text=True
    )
    assert bad.returncode != 0


# --- cleanup: the policy is run on a simulated list of versions ---


def _simulated_versions() -> list[dict[str, Any]]:
    def v(vid: int, tags: list[str], updated: str) -> dict[str, Any]:
        return {"id": vid, "updated_at": updated, "metadata": {"container": {"tags": tags}}}

    old, new = "2026-01-01T00:00:00Z", "2999-01-01T00:00:00Z"
    return [
        v(1, ["pr-12"], old),  # doomed
        v(2, ["pr-12", "pr-13"], old),  # doomed: every tag is pr-*
        v(3, ["pr-14"], new),  # too recent
        v(4, [], old),  # untagged digest: index child or attestation
        v(5, ["edge"], old),
        v(6, ["sha-abc1234"], old),
        v(7, ["1.2"], old),
        v(8, ["1.2.3"], old),
        v(9, ["latest"], old),
        v(10, ["pr-15", "edge"], old),  # mixed: kept
        v(11, ["build-abc-amd64"], old),
        v(12, ["pr-16", "latest"], old),  # mixed: kept
    ]


@pytest.mark.req("ENF-QUAL-007")
def test_ghcr_cleanup_selects_only_pr_only_old_versions() -> None:
    import json
    import subprocess

    jq = require_tool("jq")
    (job,) = _load("cleanup.yml")["jobs"].values()
    run = _text(job)
    program = re.search(r"jq -r --arg cutoff \"\$cutoff\" '(.*?)'", run, re.S)
    assert program is not None
    out = subprocess.run(  # noqa: S603
        [jq, "-r", "--arg", "cutoff", "2026-06-01T00:00:00Z", program.group(1)],
        input=json.dumps(_simulated_versions()),
        capture_output=True,
        text=True,
        check=True,
    )
    selected = sorted(int(line.split()[0]) for line in out.stdout.splitlines())
    assert selected == [1, 2]


@pytest.mark.req("ENF-QUAL-007")
def test_ghcr_cleanup_is_scheduled_with_a_dry_run() -> None:
    workflow = _load("cleanup.yml")
    triggers = workflow[True]
    assert "schedule" in triggers
    assert triggers["workflow_dispatch"]["inputs"]["dry-run"]["default"] is True
    (job,) = workflow["jobs"].values()
    assert "DRY_RUN" in _text(job)
    assert not (workflow.get("permissions") or {})
