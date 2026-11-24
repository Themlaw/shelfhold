"""`rpc_stdio`: a plugin in a subprocess, spoken to over its standard input and output (D12).

The subprocess is the plugin's alone: a closed environment (no `SHELFHOLD_*` variable and nothing
that looks like a secret; a short list of names the interpreter needs), its own session, a core
file of zero bytes, and the limits of `RpcLimits` as process limits (address space, CPU time),
with a ceiling on the processes (`MAX_TASKS`) and on the size of a file (`MAX_FILE_BYTES`). The
command line is the caller's: the runner (L05) says where the plugin's files are and how the
process is cut off from the rest; this module only starts it and ties its end to a `Channel`.
"""

import asyncio
import contextlib
import os
import re
import resource
import signal
from collections.abc import Callable, Mapping, Sequence

from shelfhold.plugin_host.rpc.protocol import Channel, RpcLimits

__all__ = ["MAX_FILE_BYTES", "MAX_TASKS", "closed_environment", "spawn_stdio"]

# `RLIMIT_NPROC` is compared with every process and thread the uid has, in the kernel's whole
# user namespace, and the plugins of one companion share a uid: this is a ceiling on the
# companion's container (it has a uid of its own there), where a fork bomb stops while the
# container's `pids_limit` is a backstop. 512 leaves room for sixteen plugins with their thread
# pools. On a host where the uid does other work (a test machine) it is too low: the runner's
# configuration (`RunnerConfig.max_tasks`) raises it.
MAX_TASKS = 512
# The largest file a plugin may write or grow (`SIGXFSZ`, then `EFBIG`): its scratch folder is a
# bounded `tmpfs`, and no plugin has a reason to build a file near the size of that `tmpfs`.
MAX_FILE_BYTES = 256 << 20

_KEEP = ("PATH", "LANG", "LC_ALL", "TZ")
_SECRETISH = re.compile(r"SECRET|PASSWORD|PASSPHRASE|TOKEN|KEY|CREDENTIAL|_FILE$", re.IGNORECASE)
_FIXED = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1"}


def closed_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The only environment a plugin's process gets: a closed list of names, never a variable of
    Shelfhold's and never one that looks like a secret, whatever the caller's environment holds."""
    env = os.environ if source is None else source
    kept = {
        k: v
        for k, v in env.items()
        if k in _KEEP and not k.startswith("SHELFHOLD_") and not _SECRETISH.search(k)
    }
    return {**kept, **_FIXED}


def _limits(limits: RpcLimits, tasks: int, confine: Callable[[], None] | None) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_NPROC, (tasks, tasks))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
    os.umask(0o077)
    if confine is not None:
        confine()  # last: once it is bound, this process may not undo what it has set


def _describe(code: int | None) -> str:
    if code is None:
        return "the plugin closed the connection"
    if code < 0:
        number = -code
        try:
            known = signal.Signals(number)
        except ValueError:
            return f"killed by signal {number}"
        cause = {signal.SIGXCPU: " (cpu time limit)", signal.SIGSEGV: " (memory limit?)"}
        return f"killed by {known.name}{cause.get(known, '')}"
    return f"exited with code {code}"


async def spawn_stdio(
    argv: Sequence[str],
    *,
    limits: RpcLimits,
    env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    confine: Callable[[], None] | None = None,
    max_tasks: int = MAX_TASKS,
) -> Channel:
    """Start `argv` as a plugin's process and connect to its standard streams. `env` is a source
    to pick from (`closed_environment`), never what the process receives as it is; `extra_env` is
    what the caller adds (the runner's scratch folder). `confine` runs in the child, after the
    limits and before `exec`: the runner's Landlock sandbox. `max_tasks` is `RLIMIT_NPROC`."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**closed_environment(env), **(extra_env or {})},
        start_new_session=True,
        preexec_fn=lambda: _limits(limits, max_tasks, confine),
    )
    stdin, stdout = process.stdin, process.stdout
    if stdin is None or stdout is None:  # pragma: no cover - PIPE always gives both
        raise RuntimeError("the plugin process has no standard streams")

    def kill_group() -> None:
        # the plugin's own session: its pid is its group's id, and a child it left behind is in it
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)

    async def exit_reason() -> str | None:
        try:
            code = await asyncio.wait_for(process.wait(), 2.0)
        except TimeoutError:
            return "the plugin closed the connection"
        kill_group()  # the plugin is gone: whatever it started goes with it
        return _describe(code)

    async def close() -> None:
        with contextlib.suppress(Exception):
            stdin.close()
        kill_group()  # also once the plugin has exited: its children may not have
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), 5.0)

    return Channel(stdout, stdin, close, exit_reason, pid=process.pid)
