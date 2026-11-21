"""M6 L05 (D-P1, EF-SRC-016, EF-USR-024, ENF-SEC-009): the companion on the image under test.

The stack of `e2e/compose.yaml` runs the core (uid 1000) and the companion `plugin-runner` (uid
1001, no network, read-only root, only the plugins' folder and the socket's). The three properties
of pass 13 are read from inside the companion, then from inside a plugin's own process, which the
core starts through the companion's socket: another uid than the core's, neither `/data` nor the
CLI socket, no interface but `lo`, no outbound connection, and an environment without a
`SHELFHOLD_*` variable. Synthetic data only; nothing leaves the stack.
"""

import json
import subprocess
from pathlib import Path

import pytest

from conftest import COMPOSE_FILE, compose, exec_python

E2E = Path(__file__).resolve().parents[1]
PROBE = (E2E / "fakes" / "m6" / "probe_plugin.py").read_text(encoding="utf-8")
SITE = "/data/plugins/site-packages"

SEES = """
import json, os
paths = ['/data', '/store', '/library', '/data/shelfhold.sock']
try:
    open('/runner-write-probe', 'w')
    writable = True
except OSError:
    writable = False
print(json.dumps({
    'uid': os.getuid(),
    'visible': [p for p in paths if any(os.access(p, m) for m in (os.R_OK, os.W_OK, os.X_OK))],
    'interfaces': sorted(os.listdir('/sys/class/net')),
    'root_writable': writable,
    'env': sorted(os.environ),
}))
"""

CONNECT = """
import asyncio, json, os, sys
from shelfhold.plugin_host.adapters.rpc.runner_socket import connect_runner
from shelfhold.plugin_host.rpc.host_side import RpcSession
from shelfhold.plugin_host.rpc.protocol import RpcLimits

async def main(query):
    limits = RpcLimits()
    channel = await connect_runner('/plugin-rpc/runner.sock', key='e2e-probe',
                                   entry='e2e_probe:make', limits=limits)
    session = await RpcSession.open(channel, key='e2e-probe', limits=limits)
    page = await session.call('source.search', {'q': query, 'filters': {'core_uid': os.getuid()},
                                                'cursor': None}, fetcher=None)
    await session.close()
    print(json.dumps({'core_uid': os.getuid(), 'seen': json.loads(page.items[0].title)}))

asyncio.run(main(sys.argv[1]))
"""
ISOLATION = {
    "isolation_no_data", "isolation_no_cli_socket", "isolation_env_closed",
    "isolation_uid_distinct",
}  # fmt: skip


def ask(query: str) -> dict[str, object]:
    """The core asks the companion for the probe plugin and puts `query` to it."""
    install_probe()
    done = compose("exec", "-T", "shelfhold", "python", "-c", CONNECT, query)
    answer = json.loads(done.stdout)
    assert isinstance(answer, dict)
    return answer


def install_probe() -> None:
    """The administrator's side of the volume: the core writes the plugin where the companion
    reads (through the `plugins` volume; the core's own write permission, nothing else)."""
    script = (
        f"umask 022; mkdir -p {SITE}; cat > {SITE}/e2e_probe.py; chmod 755 /data/plugins {SITE}; "
        f"chmod 644 {SITE}/e2e_probe.py"
    )
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T", "shelfhold",
            "sh", "-c", script,
        ],
        input=PROBE, text=True, check=True, capture_output=True, timeout=60,
    )  # fmt: skip


@pytest.mark.req("EF-SRC-016", "EF-USR-024", "ENF-SEC-009")
def test_the_compose_of_the_stack_starts_the_companion_and_it_sees_nothing_of_the_core(
    stack: None,
) -> None:
    ps = compose("ps", "--format", "json", "plugin-runner").stdout
    assert json.loads(ps.splitlines()[0])["Health"] == "healthy"
    seen = json.loads(exec_python("plugin-runner", SEES))
    assert seen["uid"] == 1001
    assert seen["visible"] == []  # no /data, no /store, no /library, no CLI socket
    assert seen["interfaces"] == ["lo"]
    assert seen["root_writable"] is False
    # the companion keeps the image's own SHELFHOLD_* settings (paths, no secret); what the
    # plugins get is a closed environment, read below from inside a plugin's process
    core = json.loads(exec_python("shelfhold", "import os; print(os.getuid())"))
    assert core == 1000 and core != seen["uid"]
    check = compose(
        "exec", "-T", "plugin-runner", "shelfhold", "plugin-runner", "--check"
    )
    assert check.returncode == 0  # the self-check passes again, from the outside


@pytest.mark.req("EF-SRC-016", "EF-USR-024", "ENF-SEC-009", "EF-SRC-019")
def test_the_core_runs_a_plugin_in_the_companion_and_the_plugin_sees_nothing(
    stack: None,
) -> None:
    answer = ask("probe")
    seen = answer["seen"]
    assert seen["uid"] == 1001 and seen["uid"] != answer["core_uid"]
    assert seen["visible"] == []
    assert seen["interfaces"] == ["lo"]
    assert seen["outbound"] is False
    assert not [n for n in seen["env_names"] if n.startswith("SHELFHOLD_")]
    # the kit's isolation check, played in the plugin's own process: every line is green
    assert seen["isolation"] and all(seen["isolation"].values()), seen["isolation"]
    # and the core, for its part, listens nowhere on the volume: it connected
    listening = exec_python(
        "shelfhold", "import os; print(sorted(os.listdir('/plugin-rpc')))"
    )
    assert "runner.sock" in listening


@pytest.mark.req("EF-SRC-016", "EF-SRC-019", "ENF-SEC-009")
def test_a_plugin_cannot_write_in_the_socket_folder_nor_take_the_socket_over(
    stack: None,
) -> None:
    """The plugin's process has the companion's uid, and the socket's folder is that uid's: what
    keeps it from replacing `runner.sock` is Landlock, in the process the companion starts."""
    seen = ask("write")["seen"]
    assert isinstance(seen, dict)
    for name in (
        "create_in_socket_folder", "unlink_socket", "rename_socket", "link_socket",
        "bind_in_socket_folder",
    ):  # fmt: skip
        assert seen[name] == "PermissionError", (name, seen)
    assert seen["plant_in_plugins"] != "ok"  # read-only volume, and Landlock besides
    assert seen["write_root"] != "ok"  # the root is read-only, as it was
    assert (
        seen["scratch"] == "ok"
    )  # the folder the companion gave it is the one place it writes
    # the socket is the companion's still, and the core still reaches it
    listing = exec_python(
        "shelfhold", "import os; print(sorted(os.listdir('/plugin-rpc')))"
    )
    assert listing.strip() == "['runner.sock']", listing
    assert ask("probe")["seen"]["uid"] == 1001  # type: ignore[index]


@pytest.mark.req("EF-USR-024", "EF-SRC-016", "ENF-QUAL-003")
def test_the_kit_in_isolated_mode_passes_for_a_shipped_plugin_in_the_companion(
    stack: None,
) -> None:
    """The kit's isolated mode, on what a plugin's process really sees in the companion (nothing
    is described by a test), for the Project Gutenberg source the repository ships."""
    kit = ask("kit")["seen"]
    assert isinstance(kit, dict)
    assert kit["ok"], kit["failures"]
    assert ISOLATION <= set(kit["names"])


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_what_a_plugin_started_and_abandoned_is_killed_with_its_connection(
    stack: None,
) -> None:
    """A grandchild in a session of its own (its parent gone) is the companion's to kill once the
    core has hung up: the companion is the subreaper of its container."""
    seen = ask("escape")["seen"]
    assert isinstance(seen, dict)
    pid = int(seen["pid"])
    script = (
        "import os, time\n"
        "for _ in range(100):\n"
        f"    if not os.path.exists('/proc/{pid}'): break\n"
        "    time.sleep(0.1)\n"
        f"print(os.path.exists('/proc/{pid}'))\n"
    )
    assert exec_python("plugin-runner", script).strip() == "False"
