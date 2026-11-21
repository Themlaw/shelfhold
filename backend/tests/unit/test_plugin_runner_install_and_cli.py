"""M6 L05 (EF-SRC-019, ENF-SEC-009, D-P1): what the core leaves on the volume the companion reads,
and the command that starts the companion."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from shelfhold.plugin_host import install as guard
from tests.plugin_guard_env import CANARY, plugin_wheel

CODE = "import sys; from shelfhold.interfaces.cli import main; sys.exit(main(sys.argv[1:]))"


def modes(root: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        out[path.relative_to(root.parent).as_posix()] = stat.S_IMODE(path.lstat().st_mode)
    return out


@pytest.mark.req("EF-SRC-019", "EF-SRC-016", "ENF-SEC-009")
def test_installed_wheels_are_readable_by_the_runner_and_writable_by_nobody_else(
    tmp_path: Path,
) -> None:
    """The core runs with `umask 077`; the companion is another uid and reads what the core put
    down. Nothing is writable by a group or others; the guard (hashes, no dependencies, the
    digest of what was installed) is the same."""
    old = os.umask(0o077)
    try:
        layout = guard.Layout.of(tmp_path / "data")
        wheel = plugin_wheel(tmp_path, "demo-plug", "demo", tmp_path / "probe.jsonl", "demo_plug")
        installed = guard.install_wheel(layout, wheel.path, wheel.sha256)
    finally:
        os.umask(old)
    seen = modes(layout.root)
    for name, mode in seen.items():
        if name.endswith("state.json"):
            assert mode == 0o600, (name, oct(mode))  # the state is the core's own
            continue
        path = layout.root.parent / name
        assert not mode & 0o022, (name, oct(mode))  # nobody else writes
        assert mode & (0o055 if path.is_dir() else 0o044), (name, oct(mode))  # others may read
    assert guard.current_digest(layout, installed) == installed.digest  # the digest is of content
    assert not [n for n in seen if ".staging" in n]


@pytest.mark.req("EF-SRC-019", "EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_command_refuses_to_start_outside_a_sandbox(tmp_path: Path) -> None:
    """On a machine with a writable root and a network, as this one is, the companion refuses to
    start, says why, binds nothing and never touches `/data`: it needs no settings, no database."""
    socket_path = tmp_path / "rpc.sock"
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", CODE, "plugin-runner", "--socket", str(socket_path),
         "--plugins", str(tmp_path / "plugins")],
        env={"PATH": os.environ["PATH"], "SHELFHOLD_DATA_DIR": str(tmp_path / "nope"),
             "SHELFHOLD_API_TOKEN": CANARY},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )  # fmt: skip
    assert done.returncode == 3, (done.stdout, done.stderr)
    assert not socket_path.exists()
    assert not (tmp_path / "nope").exists()  # it did not read the data folder's settings
    assert CANARY not in done.stdout + done.stderr
    assert "plugin-runner" in done.stderr or "companion" in done.stderr.lower()


@pytest.mark.req("EF-SRC-016", "ENF-SEC-009")
def test_plugin_runner_check_flag_reports_and_exits(tmp_path: Path) -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", CODE, "plugin-runner", "--check", "--plugins", str(tmp_path)],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 3  # this machine is not a sandbox
    assert "read-only" in done.stderr or "root" in done.stderr


@pytest.mark.req("EF-SRC-016")
def test_plugin_runner_is_a_command_of_the_cli() -> None:
    from shelfhold.interfaces.cli.registry import build_parser

    parser = build_parser()
    args = parser.parse_args(["plugin-runner", "--check"])
    assert args.command == "plugin-runner" and args.check is True
