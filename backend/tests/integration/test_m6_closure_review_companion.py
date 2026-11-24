"""M6 closure review: plugins in the companion can neither signal nor read one another.

The plugins of one companion share its uid. Each test states what the confinement promises and is
red on the head of `main`:

- a plugin cannot send a signal to the companion, to another plugin, or through a pidfd (it may
  still signal itself);
- a plugin cannot read, nor list, the scratch folder of another plugin;
- on a kernel whose Landlock stops before ABI 3 (`truncate` and `utime` are not handled there) the
  companion does not start a plugin at all;
- a plugin's process has a ceiling on its processes and on the size of a file it writes;
- the core checks the uid of the companion it connected to, as the kernel reports it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from shelfhold.plugin_host.adapters.rpc.runner_socket import RunnerUnavailable, connect_runner
from shelfhold.plugin_host.rpc.protocol import RpcLimits
from shelfhold.plugin_host.runner import confine
from shelfhold.plugin_host.runner.selfcheck import problems_of
from tests.unit.test_plugin_runner_confinement import (
    CLEAN,
    install,
    run,
    search,
    server_in,
    session,
)

pytestmark = pytest.mark.req("EF-SRC-016", "ENF-SEC-009")


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="rc", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


needs_landlock = pytest.mark.skipif(
    confine.landlock_abi() < 3, reason="needs a kernel with Landlock ABI 3 (truncate)"
)
needs_seccomp = pytest.mark.skipif(not confine.seccomp_ready(), reason="seccomp is not usable here")

PROBE = """
import ctypes, json, os, resource, signal
from rpc_plugin import Plugin, _ref

class Probe(Plugin):
    async def search(self, f, q, filters, cursor, *, sub=None):
        from shelfhold_sdk.models import Page
        ask = json.loads(q)
        out = []

        def attempt(name, action):
            try:
                result = action()
                out.append(f"{name}:{result or 'ok'}")
            except (OSError, ValueError) as exc:
                out.append(f"{name}:{type(exc).__name__}")

        if ask["do"] == "info":
            scratch = os.environ["TMPDIR"]
            with open(os.path.join(scratch, "token.txt"), "w") as handle:
                handle.write("session-token")
            out = [scratch, str(os.getpid())]
        elif ask["do"] == "signal":
            attempt("companion", lambda: os.kill(os.getppid(), signal.SIGUSR1))
            attempt("sibling", lambda: os.kill(ask["pid"], signal.SIGKILL))
            attempt("sibling-probe", lambda: os.kill(ask["pid"], 0))

            def through_tgkill():
                libc = ctypes.CDLL(None, use_errno=True)
                if libc.tgkill(ask["pid"], ask["pid"], 0) != 0:
                    raise PermissionError(ctypes.get_errno(), "tgkill")

            attempt("sibling-tgkill", through_tgkill)
            attempt("everyone", lambda: os.kill(-1, signal.SIGUSR1))

            def through_a_pidfd():
                fd = os.pidfd_open(ask["pid"])
                signal.pidfd_send_signal(fd, signal.SIGKILL)

            attempt("pidfd", through_a_pidfd)
            signal.signal(signal.SIGUSR2, lambda *_: None)
            attempt("itself", lambda: os.kill(os.getpid(), signal.SIGUSR2))
            attempt("raise", lambda: signal.raise_signal(signal.SIGUSR2))
        elif ask["do"] == "read":
            attempt("token", lambda: open(os.path.join(ask["scratch"], "token.txt")).read()[:0])
            attempt("listing", lambda: os.listdir(ask["scratch"]) and None)
            attempt("own", lambda: open(os.path.join(os.environ["TMPDIR"], "token.txt")).read()[:0])
            attempt("urandom", lambda: open("/dev/urandom", "rb").read(1)[:0])
        elif ask["do"] == "limits":
            for name in ("RLIMIT_NPROC", "RLIMIT_FSIZE"):
                soft, hard = resource.getrlimit(getattr(resource, name))
                out.append(f"{name}:{soft}:{hard}")
        return Page(items=[_ref(x) for x in out])

def make():
    return Probe()
"""


async def ask(s, **what: object) -> list[str]:  # type: ignore[no-untyped-def]
    return await search(s, json.dumps(what))


@needs_landlock
@needs_seccomp
def test_a_plugin_signals_neither_the_companion_nor_another_plugin(short_dir: Path) -> None:
    install(short_dir, "probe", PROBE)
    got: list[int] = []
    previous = signal.signal(signal.SIGUSR1, lambda number, _frame: got.append(number))

    async def scenario() -> None:
        server = server_in(short_dir)
        await server.start()
        try:
            a = await session(server, "rpc-demo", "probe:make")
            b = await session(server, "rpc-demo-2", "probe:make")
            _scratch, b_pid = await ask(b, do="info")
            result = dict(item.split(":", 1) for item in await ask(a, do="signal", pid=int(b_pid)))
            await asyncio.sleep(0.2)
            assert got == [], "a plugin signalled the companion"
            for name in ("companion", "sibling", "sibling-probe", "sibling-tgkill", "everyone"):
                assert result[name] == "PermissionError", (name, result)
            assert result["pidfd"] == "PermissionError", result
            assert (await ask(b, do="info"))[1] == b_pid  # the other plugin is still there
            assert result["itself"] == "ok" and result["raise"] == "ok", result
            await a.close()
            await b.close()
        finally:
            await server.close()

    try:
        run(scenario)
    finally:
        signal.signal(signal.SIGUSR1, previous)


FALLBACK = """
import os, signal, subprocess, sys
sibling = int(sys.argv[1])
out = {}
def attempt(name, action):
    try:
        action(); out[name] = "ok"
    except OSError as exc:
        out[name] = type(exc).__name__
signal.signal(signal.SIGUSR2, lambda *_: None)
attempt("companion", lambda: os.kill(os.getppid(), signal.SIGUSR1))
attempt("sibling", lambda: os.kill(sibling, signal.SIGKILL))
attempt("everyone", lambda: os.kill(-1, signal.SIGUSR1))
attempt("itself", lambda: os.kill(os.getpid(), signal.SIGUSR2))
attempt("own group", lambda: os.killpg(os.getpgrp(), signal.SIGUSR2))
attempt("raise", lambda: signal.raise_signal(signal.SIGUSR2))
attempt("pidfd", lambda: signal.pidfd_send_signal(os.pidfd_open(sibling), signal.SIGKILL))
print(out)
"""


@needs_seccomp
def test_without_landlock_scopes_seccomp_refuses_the_signals_to_others() -> None:
    """On a kernel older than 6.12 the filter is what keeps a plugin from signalling."""
    got: list[int] = []
    previous = signal.signal(signal.SIGUSR1, lambda number, _frame: got.append(number))
    sibling = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
    filt = confine.Filter(signals=True)  # type: ignore[call-arg]
    try:
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-c", FALLBACK, str(sibling.pid)],
            preexec_fn=filt.install, start_new_session=True, capture_output=True, text=True,
            timeout=60, check=False,
        )  # fmt: skip
        assert done.returncode == 0, done.stderr
        seen = eval(done.stdout)  # noqa: S307 - our own child's dict
        for name in ("companion", "sibling", "everyone", "pidfd"):
            assert seen[name] == "PermissionError", seen
        assert seen["itself"] == seen["own group"] == seen["raise"] == "ok", seen
        assert got == [] and sibling.poll() is None
    finally:
        signal.signal(signal.SIGUSR1, previous)
        sibling.kill()
        sibling.wait()


@needs_landlock
def test_a_plugin_reads_nothing_of_the_scratch_folder_of_another(short_dir: Path) -> None:
    install(short_dir, "probe", PROBE)

    async def scenario() -> None:
        server = server_in(short_dir)
        await server.start()
        try:
            a = await session(server, "rpc-demo", "probe:make")
            b = await session(server, "rpc-demo-2", "probe:make")
            await ask(a, do="info")
            b_scratch, _pid = await ask(b, do="info")
            result = dict(item.split(":", 1) for item in await ask(a, do="read", scratch=b_scratch))
            assert result["token"] == "PermissionError"  # noqa: S105, result
            assert result["listing"] == "PermissionError", result
            assert result["own"] == "ok" and result["urandom"] == "ok", result
            await a.close()
            await b.close()
        finally:
            await server.close()

    run(scenario)


def test_a_kernel_whose_landlock_stops_before_truncate_starts_no_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Landlock ABI 1 and 2 do not handle `truncate` and `utime`: a plugin could empty a file of
    another plugin (or of the companion's volume) in place. The self-check and the ruleset both
    refuse such a kernel."""
    refused = problems_of(replace(CLEAN, landlock_abi=2))
    assert len(refused) == 1 and "ABI 3" in refused[0], refused
    assert problems_of(replace(CLEAN, landlock_abi=3)) == []
    monkeypatch.setattr(confine, "landlock_abi", lambda: 2)
    with pytest.raises(OSError, match="ABI 3"):
        confine.Ruleset([tmp_path])


@needs_landlock
def test_a_plugin_has_a_ceiling_on_its_processes_and_on_the_files_it_writes(
    short_dir: Path,
) -> None:
    install(short_dir, "probe", PROBE)

    async def scenario() -> None:
        server = server_in(short_dir)
        await server.start()
        try:
            a = await session(server, "rpc-demo", "probe:make")
            seen = {}
            for item in await ask(a, do="limits"):
                name, soft, hard = item.split(":")
                seen[name] = (int(soft), int(hard))
            for name, (soft, hard) in seen.items():
                assert 0 < soft == hard <= 1 << 30, (name, soft, hard)  # not RLIM_INFINITY
            assert set(seen) == {"RLIMIT_NPROC", "RLIMIT_FSIZE"}
            await a.close()
        finally:
            await server.close()

    run(scenario)


def test_the_core_checks_the_uid_of_the_companion_it_connected_to(short_dir: Path) -> None:
    """The core reads `SO_PEERCRED` of its own end: a socket whose listener is not the companion's
    uid is not spoken to (nothing is sent: the greeting names a plugin)."""
    (short_dir / "plugins").mkdir()
    me = os.getuid()

    async def scenario() -> None:
        server = server_in(short_dir)
        await server.start()
        try:
            with pytest.raises(RunnerUnavailable, match="uid"):
                await connect_runner(
                    server.config.socket_path, key="rpc-demo", entry="x:make",
                    limits=RpcLimits(), core_uid=1000, runner_uid=me + 1,  # type: ignore[call-arg]
                )  # fmt: skip
            assert server.pids() == {}
        finally:
            await server.close()

    run(scenario)
