"""Guards on the Suwayomi contract bench (ENF-LEG-013, spike S5).

The bench must never be able to reach third-party content: no extension store, no
network egress, an image pinned by digest, and only Suwayomi's local source.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "e2e" / "suwayomi" / "compose.yaml"
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


@pytest.mark.req("ENF-LEG-013")
def test_contract_job_has_no_extension_repo_and_no_egress() -> None:
    compose = _load(COMPOSE)
    # Every network is internal: no route out of the bench.
    networks = compose["networks"]
    assert networks
    assert all(net.get("internal") is True for net in networks.values())
    for service in compose["services"].values():
        assert "ports" not in service, "no published port: the bench stays on the internal net"
        assert "network_mode" not in service
        for forbidden in (
            "dns",
            "extra_hosts",
            "cap_add",
            "privileged",
            "devices",
            "external_links",
        ):
            assert forbidden not in service, forbidden
        assert set(service["networks"]) <= set(networks)
        assert DIGEST_IMAGE.match(service["image"]), "image must be pinned by digest"
    # No extension store/repo configured anywhere; the explicit list is empty.
    suwayomi_env = _service_env(compose["services"]["suwayomi"])
    assert suwayomi_env["EXTENSION_STORES"] == "[]"
    for name in suwayomi_env:
        if name != "EXTENSION_STORES":
            assert not FORBIDDEN_ENV.search(name), name
