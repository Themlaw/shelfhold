"""Supply chain of the release workflows (ENF-SEC-007 palier M1b, ENF-QUAL-007).

The workflows are read as YAML, never run: provenance and SBOM attestations on the pushed digest,
release proofs attached as assets, Grype and CodeQL, and a GHCR cleanup that can only touch the
`pr-*` tags. Every action is pinned by SHA and the write scopes of the OIDC token stay on one job.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

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
    assert "gh attestation" in text
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


@pytest.mark.req("ENF-SEC-007")
def test_attestation_permissions_scoped_to_publish_job() -> None:
    """Original name: test_attestation_permissions_scoped_to_publish_job."""
    holders = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        top = workflow.get("permissions") or {}
        assert "id-token" not in top and "attestations" not in top, path.name
        for name, job in workflow["jobs"].items():
            perms = job.get("permissions")
            if isinstance(perms, dict) and ("id-token" in perms or "attestations" in perms):
                holders.append((path.name, name, dict(perms)))
    assert holders == [
        (
            "image-publish.yml",
            "merge",
            {
                "contents": "read",
                "packages": "write",
                "id-token": "write",
                "attestations": "write",
            },
        )
    ]
    # the callers grant the same scopes, to that reusable workflow only
    ci = _load("ci.yml")["jobs"]["image-publish"]["permissions"]
    assert ci["id-token"] == "write" and ci["attestations"] == "write"
    assert "uses" in _load("ci.yml")["jobs"]["image-publish"]
    release_jobs = _load("release.yml")["jobs"]
    assert "id-token" not in str(release_jobs)


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
    assert "scan" not in ci["ci-success"]["needs"]


@pytest.mark.req("ENF-SEC-007")
def test_codeql_covers_python_and_typescript() -> None:
    workflow = _load("codeql.yml")
    assert workflow.get("permissions") == {}
    job = workflow["jobs"]["analyze"]
    assert job["permissions"] == {"contents": "read", "security-events": "write"}
    languages = {entry["language"] for entry in job["strategy"]["matrix"]["include"]}
    assert languages == {"python", "javascript-typescript"}
    assert "pull_request" in workflow[True] and "schedule" in workflow[True]
