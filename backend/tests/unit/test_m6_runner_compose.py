"""M6 L05 (D-P1, EF-SRC-016, EF-USR-024, ENF-SEC-009): the companion in the compose files and the
image. The companion is the same image under another uid, with no network, a read-only root, and
only two volumes: the plugins' (read-only) and the one socket. The reference and the simple
compose carry it commented out (the administrator opts in, step by step); the compose of the e2e
stack runs it, which is how the CI proves that it starts and that its self-check passes."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN_MOUNTS = ("/data", "/store", "/library", "/import", "/upstream", "/var/run/docker.sock")
BEGIN, END = "# >>> plugin-runner", "# <<< plugin-runner"


def load(path: str) -> dict[str, Any]:
    data = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def commented_blocks(path: str) -> dict[str, dict[str, Any]]:
    """The YAML hidden in the comments between the markers, uncommented: `service` (the companion)
    and `core` (what the core service gains), each a block of its own."""
    text = (ROOT / path).read_text(encoding="utf-8")
    blocks: dict[str, dict[str, Any]] = {}
    pattern = re.compile(rf"^( *){re.escape(BEGIN)} (\w+)\n(.*?)^ *{re.escape(END)}", re.M | re.S)
    for match in pattern.finditer(text):
        indent, name, body = match.group(1), match.group(2), match.group(3)
        lines = []
        for line in body.splitlines():
            stripped = line.removeprefix(indent)
            assert stripped.startswith("#"), f"{path}: {line!r} is not a comment"
            lines.append(stripped.removeprefix("# ").removeprefix("#"))
        loaded = yaml.safe_load("\n".join(lines))
        assert isinstance(loaded, dict), (path, name)
        blocks[name] = loaded
    return blocks


def assert_companion(svc: dict[str, Any]) -> None:
    assert svc["user"] == "1001:1001"  # not the core's uid (1000)
    assert svc["network_mode"] == "none"  # no network at all, not even a private one
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert "ports" not in svc and "env_file" not in svc and "environment" not in svc
    assert svc["command"] == ["shelfhold", "plugin-runner"]
    mounts = [v if isinstance(v, str) else f"{v['source']}:{v['target']}" for v in svc["volumes"]]
    targets = sorted(m.split(":")[1] for m in mounts)
    assert targets == ["/plugin-rpc", "/plugins"], mounts  # the two volumes, nothing else
    plugins = next(m for m in mounts if m.split(":")[1] == "/plugins")
    assert plugins.endswith(":ro")  # the core installs; the companion only reads
    for forbidden in FORBIDDEN_MOUNTS:
        assert not any(forbidden in m for m in mounts), (forbidden, mounts)
    assert svc["ulimits"]["core"] == 0
    assert svc["mem_limit"] and int(svc["pids_limit"]) > 0


@pytest.mark.req("EF-SRC-016", "EF-USR-024", "ENF-SEC-009")
def test_plugin_runner_service_has_no_data_no_network_and_another_uid() -> None:
    """e2e/compose.yaml: the companion runs, with the three properties of pass 13."""
    compose = load("e2e/compose.yaml")
    svc = compose["services"]["plugin-runner"]
    assert_companion(svc)
    assert "${SHELFHOLD_IMAGE" in svc["image"]  # the image under test: the core's image, not another
    assert compose["services"]["shelfhold"]["user"] == "1000:1000"
    assert set(compose["volumes"]) >= {"plugins", "plugin-rpc"}
    # the core is the one that installs (writes the plugins) and connects (the socket)
    core = [v.split(":")[:2] for v in compose["services"]["shelfhold"]["volumes"]]
    assert ["plugins", "/data/plugins"] in core and ["plugin-rpc", "/plugin-rpc"] in core
    assert "depends_on" in compose["services"]["shelfhold"]  # starts after the companion
    assert "plugin-runner" in compose["services"]["shelfhold"]["depends_on"]


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_is_healthy_only_when_its_socket_accepts() -> None:
    svc = load("e2e/compose.yaml")["services"]["plugin-runner"]
    health = svc["healthcheck"]
    assert health["test"][0] == "CMD" and "plugin-rpc" in " ".join(health["test"])
    assert load("e2e/compose.yaml")["services"]["shelfhold"]["depends_on"]["plugin-runner"] == {
        "condition": "service_healthy"
    }


@pytest.mark.req("EF-SRC-016", "EF-USR-024", "ENF-SEC-009")
@pytest.mark.parametrize("path", ["compose.yaml", "compose.simple.yaml"])
def test_plugin_runner_is_off_by_default_and_complete_when_uncommented(path: str) -> None:
    active = load(path)
    assert list(active["services"]) == ["shelfhold"]  # nothing changes for an existing install
    assert "plugin-runner" not in (ROOT / path).read_text(encoding="utf-8").split(BEGIN)[0]
    blocks = commented_blocks(path)
    assert set(blocks) == {"service", "core"}
    assert_companion(blocks["service"]["plugin-runner"])
    # the core gains the socket's folder, and nothing else: the plugins are already in its data
    # folder, which the companion reads through a mount of that one sub-folder (`./data/plugins`)
    core_volumes = blocks["core"]["volumes"]
    assert [v.split(":")[1] for v in core_volumes] == ["/plugin-rpc"]
    companion = {v.split(":")[1]: v for v in blocks["service"]["plugin-runner"]["volumes"]}
    assert companion["/plugin-rpc"] == core_volumes[0]  # the same folder: one socket
    assert companion["/plugins"] == "./data/plugins:/plugins:ro"


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_steps_are_written_for_the_owner() -> None:
    """The steps (D-P1: an action outside the repository) are in the file, in order."""
    for path in ("compose.yaml", "compose.simple.yaml"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "docs/admin/plugins-isoles.md" in text
        assert text.count(BEGIN) == 2 and text.count(END) == 2
    doc = (ROOT / "docs/admin/plugins-isoles.md").read_text(encoding="utf-8")
    for needle in ("plugin-runner", "1001", "network_mode: none", "/plugin-rpc", "data/plugins"):
        assert needle in doc


@pytest.mark.req("EF-SRC-016", "EF-USR-024", "ENF-SEC-009")
def test_dockerfile_adds_the_companion_user_and_leaves_the_core_alone() -> None:
    text = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"useradd --uid 1001 .* plugin-runner|useradd[^\n]*--uid 1001", text)
    assert re.search(r"^USER 1000:1000$", text, flags=re.MULTILINE)  # the core is unchanged
    assert text.count("USER ") == 1
    assert 'CMD ["shelfhold", "serve", "--migrate"]' in text
    # 1001 owns nothing the core owns
    assert not re.search(r"chown[^\n]*1001[^\n]*/(data|store)", text)
