"""Static checks of release.yml and cleanup.yml (ENF-SEC-007, ENF-OPS-002, ENF-QUAL-007)."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPTS = ROOT / ".github" / "scripts"
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}( #.*)?$")


def load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def triggers(wf: dict[str, Any]) -> set[str]:
    return set(_on(wf))


def _on(wf: dict[Any, Any]) -> dict[str, Any]:
    on = wf.get("on", wf.get(True))  # PyYAML reads the bare key `on` as True
    assert isinstance(on, dict)
    return on


@pytest.mark.parametrize("name", ["release.yml", "cleanup.yml"])
def test_actions_are_pinned_by_sha_and_token_defaults_to_nothing(name: str) -> None:
    wf = load(name)
    assert wf["permissions"] == {}
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                assert PINNED.match(f"{step['uses']} # x"), step["uses"]


def test_release_runs_on_push_to_main_only() -> None:
    wf = load("release.yml")
    assert triggers(wf) == {"push"}
    assert _on(wf)["push"]["branches"] == ["main"]
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "pull_request" not in code.replace("pull-requests", "")


def test_release_permissions_are_minimal_per_job() -> None:
    jobs = load("release.yml")["jobs"]
    assert jobs["release-please"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }
    tags = jobs["image-tags"]
    assert tags["permissions"] == {"contents": "read", "packages": "write", "checks": "read"}
    # the commit of the release, never github.sha (the run's own commit)
    assert tags["with"]["sha"] == "${{ needs.release-please.outputs.sha }}"
    assert "github.sha" not in (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert tags["uses"] == "./.github/workflows/image-retag.yml"
    assert jobs["image-tags"]["if"] == "needs.release-please.outputs.release_created == 'true'"
    rp = [
        s for s in jobs["release-please"]["steps"] if "release-please-action" in s.get("uses", "")
    ]
    assert len(rp) == 1 and "@v" not in rp[0]["uses"].split(" ")[0]


def test_release_retags_the_published_digest_never_latest_never_rebuilds() -> None:
    for path in (
        WORKFLOWS / "release.yml",
        WORKFLOWS / "retag.yml",
        WORKFLOWS / "image-retag.yml",
        SCRIPTS / "release-retag.sh",
        WORKFLOWS / "cleanup.yml",
    ):
        text = path.read_text(encoding="utf-8")
        assert ":latest" not in text and "-t latest" not in text, path.name
        assert (
            "build-push-action" not in text
            and "docker build " not in text
            and "buildx build" not in text
        )
    script = (SCRIPTS / "release-retag.sh").read_text(encoding="utf-8")
    assert "sha-${SHA}" in script and "imagetools create" in script
    assert "refusing to rewrite" in script  # existing X.Y.Z with another digest fails
    assert "WAIT_SECONDS:-" in script  # bounded wait for the ci image


def test_release_please_has_no_sticky_release_as_and_requires_migration_line() -> None:
    cfg = json.loads((ROOT / "release-please-config.json").read_text(encoding="utf-8"))
    assert (
        "release-as" not in cfg["packages"]["."]
    )  # bootstrap = Release-As footer, see release.yml
    assert "Release-As: 0.1.0" in (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    script = (SCRIPTS / "release-migration-line.sh").read_text(encoding="utf-8")
    assert "Migration : " in script and "réversible : " in script
    steps = load("release.yml")["jobs"]["release-please"]["steps"]
    assert any("release-migration-line.sh" in s.get("run", "") for s in steps)


def test_manual_retag_is_a_dispatch_on_an_existing_version_tag() -> None:
    wf = load("retag.yml")
    assert triggers(wf) == {"workflow_dispatch"}
    assert wf["permissions"] == {}
    inputs = _on(wf)["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"version"} and inputs["version"]["required"] is True
    callee = load("image-retag.yml")
    assert triggers(callee) == {"workflow_call"}
    assert callee["jobs"]["retag"]["permissions"]["packages"] == "write"
    assert "checks" in callee["jobs"]["retag"]["permissions"]


def test_cleanup_never_runs_on_pull_request_and_writes_packages_on_one_job() -> None:
    wf = load("cleanup.yml")
    assert triggers(wf) == {"schedule", "workflow_dispatch"}
    writers = [n for n, j in wf["jobs"].items() if j["permissions"].get("packages") == "write"]
    assert writers == ["purge-build-tags"]
    text = (WORKFLOWS / "cleanup.yml").read_text(encoding="utf-8")
    assert _on(wf)["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    # dry-run by default, cron included: only an explicit dry_run=false deletes
    assert "|| 'true'" in text and "format('{0}', inputs.dry_run)" in text
    assert "DRY_RUN:-true" in (SCRIPTS / "ghcr-cleanup.sh").read_text(encoding="utf-8")
    assert "${OWNER,,}" in (SCRIPTS / "ghcr-cleanup.sh").read_text(encoding="utf-8")


def _select(versions: list[dict[str, Any]], protected: list[str]) -> list[int]:
    if shutil.which("jq") is None:
        pytest.skip("jq not installed")
    out = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "jq", "-c", "--arg", "cutoff", "2026-11-01T00:00:00Z",
            "--argjson", "protected", json.dumps(protected),
            "-f", str(SCRIPTS / "ghcr-cleanup.jq"),
        ],
        input=json.dumps(versions), capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return [v["id"] for v in json.loads(out)]


def _v(id_: int, tags: list[str], created: str = "2026-10-01T00:00:00Z") -> dict[str, Any]:
    return {
        "id": id_,
        "name": f"sha256:{id_}",
        "created_at": created,
        "metadata": {"container": {"tags": tags}},
    }


def test_ghcr_cleanup_selects_only_expired_unreferenced_build_tags() -> None:
    versions = [
        _v(1, ["build-abc-amd64"]),
        _v(2, ["build-abc-amd64", "edge"]),
        _v(3, ["sha-abc"]),
        _v(4, ["0.1.0"]),
        _v(5, ["0.1"]),
        _v(6, []),
        _v(7, ["build-new-arm64"], created="2026-11-03T00:00:00Z"),
        _v(8, ["build-kept-arm64"]),
        _v(9, ["build-abc-arm64", "1.2.3"]),
    ]
    assert _select(versions, ["sha256:8"]) == [1]
