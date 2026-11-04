"""Guards on the Suwayomi contract bench (ENF-LEG-013, spike S5).

The bench must never be able to reach third-party content: no extension store, no
network egress, an image pinned by digest, and only Suwayomi's local source.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "contract.yml"
COMPOSE = ROOT / "e2e" / "suwayomi" / "compose.yaml"
CONTRACT_DIR = ROOT / "backend" / "tests" / "contract" / "suwayomi"
DIGEST_IMAGE = re.compile(r"^ghcr\.io/suwayomi/suwayomi-server@sha256:[0-9a-f]{64}$")
FORBIDDEN_ENV = re.compile(r"EXTENSION_(REPO|STORE)S?|EXTENSION_?URL|REPOS?_URL", re.IGNORECASE)


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _service_env(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment", {})
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return {key: str(value) for key, value in env.items()}


def test_contract_job_has_no_extension_repo_and_no_egress() -> None:
    compose = _load(COMPOSE)
    # Every network is internal: no route out of the bench.
    networks = compose["networks"]
    assert networks
    assert all(net.get("internal") is True for net in networks.values())
    for service in compose["services"].values():
        assert "ports" not in service, "no published port: the bench stays on the internal net"
        assert service.get("network_mode") not in {"host", "bridge"}
        assert set(service["networks"]) <= set(networks)
        assert DIGEST_IMAGE.match(service["image"]), "image must be pinned by digest"
    # No extension store/repo configured anywhere; the explicit list is empty.
    suwayomi_env = _service_env(compose["services"]["suwayomi"])
    assert suwayomi_env["EXTENSION_STORES"] == "[]"
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    compose_text = COMPOSE.read_text(encoding="utf-8")
    for name in suwayomi_env:
        if name != "EXTENSION_STORES":
            assert not FORBIDDEN_ENV.search(name), name
    assert not re.search(r"(?i)extension_?repo|keiyoushi|extensions\.json", compose_text)
    assert not re.search(r"(?i)extension_?repo|keiyoushi|extensions\.json", workflow_text)


def test_contract_workflow_triggers_and_permissions() -> None:
    workflow = _load(WORKFLOW)
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1 reads `on` as True
    assert set(triggers) == {"workflow_dispatch", "schedule", "pull_request"}
    assert triggers["pull_request"]["paths"] == [
        "e2e/suwayomi/**",
        "backend/tests/contract/**",
        ".github/workflows/contract.yml",
    ]
    assert workflow["permissions"] == {"contents": "read"}
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            uses = step.get("uses")
            if uses:
                assert re.search(r"@[0-9a-f]{40}$", uses), f"action not pinned by SHA: {uses}"


def test_contract_suwayomi_uses_local_source_only() -> None:
    compose = _load(COMPOSE)
    env = _service_env(compose["services"]["suwayomi"])
    # No web UI, no browser engine, no FlareSolverr: nothing that fetches or proxies.
    assert env["WEB_UI_ENABLED"] == "false"
    assert env["KCEF_ENABLED"] == "false"
    assert env["FLARESOLVERR_ENABLED"] == "false"
    # The test code only ever talks to source "0" (Suwayomi's local source) and its
    # fixtures come from the synthetic generator.
    sources = "\n".join(p.read_text(encoding="utf-8") for p in CONTRACT_DIR.glob("*.py"))
    assert 'LOCAL_SOURCE_ID = "0"' in sources
    assert "fixtures" in sources and "synthetic" in sources
    assert not re.search(
        r"mutation[^\"]*\b(installExternalExtension|updateExtensions?|fetchExtensions|addExtensionStore)",
        sources,
    )
