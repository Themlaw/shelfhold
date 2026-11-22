"""What the companion does to the processes around it (D-P1, pass 13, review of L05).

The plugins run under the companion's own uid, in its container. Docker cuts them off from the
core; this module cuts them off from the companion and from each other's leftovers:

- **Landlock** (`Ruleset`): a plugin's process may write nowhere but a scratch folder of its own
  (no file, folder, link or socket is created, removed or renamed anywhere else: not on the socket
  folder, not on the plugins volume), and, where the kernel has ABI 4, may bind or connect over TCP
  nowhere. It needs no privilege. A kernel without Landlock is a sandbox the companion refuses to
  start in (`landlock_abi`).
- **The process itself** (`harden_process`): not dumpable (a plugin of the same uid cannot read its
  environment or attach to it) and the subreaper of every process that loses its parent, so that a
  plugin which double-forks and calls `setsid()` is still the companion's to kill (`kill_tree`,
  `orphans`).

- **A seccomp filter** (`Filter`), posed beside Landlock before the plugin runs: `setsid` and
  `setpgid` do nothing (a plugin stays in the group `killpg` kills, however fast it forks), and the
  `chmod`, `chown` and `setxattr` families fail with `EPERM` (Landlock does not handle them: a
  plugin that owns the socket's folder could make it unreachable for the core). The filter is
  built by hand, for the two architectures the companion knows (x86-64, arm64); on another one it
  does not start.

Standard library only (`ctypes` for the Landlock and seccomp system calls, `/proc` for the process
tree).
"""

import contextlib
import ctypes
import errno
import os
import platform
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "Filter",
    "Ruleset",
    "children_of",
    "collect_zombies",
    "harden_process",
    "kill_tree",
    "kill_trees",
    "landlock_abi",
    "orphans",
    "reap",
    "restore_process",
    "seccomp_ready",
    "sweep",
]

_SYS_CREATE, _SYS_ADD, _SYS_RESTRICT = 444, 445, 446  # the same number on x86-64 and arm64
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_DUMPABLE, _PR_GET_DUMPABLE = 4, 3
_PR_SET_CHILD_SUBREAPER, _PR_GET_CHILD_SUBREAPER = 36, 37
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER = 22, 2

# access rights of the filesystem, by the ABI that introduced them
_WRITE_FILE, _REMOVE_DIR, _REMOVE_FILE = 1 << 1, 1 << 4, 1 << 5
_MAKES = sum(1 << n for n in range(6, 13))  # char, dir, regular, socket, fifo, block, symlink
_REFER, _TRUNCATE = 1 << 13, 1 << 14
_NET_BIND_TCP, _NET_CONNECT_TCP = 1 << 0, 1 << 1

_libc: ctypes.CDLL | None = None


def _c() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    return _libc


def _syscall(number: int, *args: object) -> int:
    return int(_c().syscall(number, *args))


def landlock_abi() -> int:
    """The Landlock ABI of this kernel (0: none, or switched off for this process)."""
    try:
        version = _syscall(_SYS_CREATE, None, 0, _CREATE_RULESET_VERSION)
    except (OSError, AttributeError):
        return 0
    return max(version, 0)


def _handled_fs(abi: int) -> int:
    rights = _WRITE_FILE | _REMOVE_DIR | _REMOVE_FILE | _MAKES
    if abi >= 2:
        rights |= _REFER
    if abi >= 3:
        rights |= _TRUNCATE
    return rights


class Ruleset:
    """A Landlock ruleset built by the companion, applied by the child before it runs the plugin.

    Everything that writes is handled, and only `writable` (folders) are granted it, beneath them;
    `/dev/null` may be written to. Reading and executing are not handled: a plugin reads what it
    is given. With ABI 4 the TCP rights are handled and granted nowhere."""

    def __init__(self, writable: Iterable[Path]) -> None:
        self.abi = landlock_abi()
        if self.abi < 1:
            raise OSError("the kernel offers no Landlock")
        handled = _handled_fs(self.abi)
        net = (_NET_BIND_TCP | _NET_CONNECT_TCP) if self.abi >= 4 else 0
        attr = struct.pack("QQ", handled, net) if self.abi >= 4 else struct.pack("Q", handled)
        self._fd = _syscall(_SYS_CREATE, ctypes.c_char_p(attr), len(attr), 0)
        if self._fd < 0:
            raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
        try:
            for folder in writable:
                self._allow(folder, handled)
            null = Path(os.devnull)
            if null.exists():
                self._allow(null, _WRITE_FILE | (_TRUNCATE if self.abi >= 3 else 0))
        except BaseException:
            self.close()
            raise

    def _allow(self, path: Path, access: int) -> None:
        opened = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = struct.pack("=Qi", access, opened)  # packed: 12 bytes, as the kernel reads it
            buffer = ctypes.create_string_buffer(rule, len(rule))
            if _syscall(_SYS_ADD, self._fd, _RULE_PATH_BENEATH, buffer, 0) != 0:
                raise OSError(ctypes.get_errno(), f"landlock_add_rule {path}")
        finally:
            os.close(opened)

    def restrict(self) -> None:
        """In the child, before `exec`: from here this process (and what it starts) is bound."""
        _prctl(_PR_SET_NO_NEW_PRIVS, 1)
        if _syscall(_SYS_RESTRICT, self._fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


def _prctl(option: int, arg: int = 0) -> int:
    return int(_c().prctl(option, arg, 0, 0, 0))


def harden_process() -> tuple[int, int]:
    """Not dumpable, and the subreaper. Returns what it was, for `restore_process`."""
    was = (_prctl(_PR_GET_DUMPABLE), _reaper_state())
    _prctl(_PR_SET_DUMPABLE, 0)
    _prctl(_PR_SET_CHILD_SUBREAPER, 1)
    return was


def _reaper_state() -> int:
    flag = ctypes.c_int(0)
    _c().prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(flag), 0, 0, 0)
    return int(flag.value)


def restore_process(was: tuple[int, int]) -> None:
    _prctl(_PR_SET_DUMPABLE, was[0])
    _prctl(_PR_SET_CHILD_SUBREAPER, was[1])


# -- seccomp -----------------------------------------------------------------------------------

_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
_X32 = 0x40000000  # on x86-64, a system call number with this bit is the x32 ABI's
_RET_ALLOW, _RET_KILL_PROCESS = 0x7FFF0000, 0x80000000
_RET_ERRNO = 0x00050000  # | errno
# what a plugin's process may not do, by architecture: the names are the kernel's
_DENIED: dict[str, dict[str, int]] = {
    "x86_64": {
        "chmod": 90, "fchmod": 91, "fchmodat": 268, "fchmodat2": 452,
        "chown": 92, "fchown": 93, "lchown": 94, "fchownat": 260,
        "setxattr": 188, "lsetxattr": 189, "fsetxattr": 190, "setxattrat": 463,
        "removexattr": 197, "lremovexattr": 198, "fremovexattr": 199, "removexattrat": 466,
        # io_uring carries its own SETXATTR, which no number above would see
        "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
    },
    "aarch64": {
        "fchmod": 52, "fchmodat": 53, "fchmodat2": 452,
        "fchown": 55, "fchownat": 54,
        "setxattr": 5, "lsetxattr": 6, "fsetxattr": 7, "setxattrat": 463,
        "removexattr": 14, "lremovexattr": 15, "fremovexattr": 16, "removexattrat": 466,
        "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
    },
}  # fmt: skip
# `setsid` and `setpgid` are refused by *not happening*: the call returns 0 and the process stays
# in its group. A plugin (or a library it starts) that daemonises with `setsid()` is then still in
# the group `killpg` kills, and does not fail on the way; `EPERM` would crash whatever called it.
_STAYS: dict[str, dict[str, int]] = {
    "x86_64": {"setpgid": 109, "setsid": 112},
    "aarch64": {"setpgid": 154, "setsid": 157},
}
_BPF_LD_W_ABS, _BPF_JEQ_K, _BPF_JGE_K, _BPF_RET_K = 0x20, 0x15, 0x35, 0x06


def _instruction(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k)


def machine() -> str | None:
    """The architecture the filter knows, or None (the companion does not start there)."""
    name = platform.machine()
    return name if name in _AUDIT_ARCH else None


def build_program(arch: str) -> bytes:
    """The BPF program: another architecture kills the process (a 32-bit call would dodge the
    numbers below), the changes of mode and owner fail with `EPERM`, `setsid` and `setpgid`
    return 0 without doing anything, everything else is allowed."""
    checks: list[tuple[int, int, bool]] = [  # (instruction, number, returns 0 and does nothing)
        *((_BPF_JEQ_K, n, False) for n in sorted(_DENIED[arch].values())),
        *((_BPF_JEQ_K, n, True) for n in sorted(_STAYS[arch].values())),
    ]
    if arch == "x86_64":
        checks.insert(0, (_BPF_JGE_K, _X32, False))  # the x32 numbers are all refused
    program = [
        _instruction(_BPF_LD_W_ABS, 0, 0, 4),  # seccomp_data.arch
        _instruction(_BPF_JEQ_K, 1, 0, _AUDIT_ARCH[arch]),
        _instruction(_BPF_RET_K, 0, 0, _RET_KILL_PROCESS),
        _instruction(_BPF_LD_W_ABS, 0, 0, 0),  # seccomp_data.nr
    ]
    for index, (code, value, stays) in enumerate(checks):
        # the instructions after the checks: allow, then EPERM, then "returns 0"
        program.append(_instruction(code, len(checks) - index + int(stays), 0, value))
    program.append(_instruction(_BPF_RET_K, 0, 0, _RET_ALLOW))
    program.append(_instruction(_BPF_RET_K, 0, 0, _RET_ERRNO | errno.EPERM))
    program.append(_instruction(_BPF_RET_K, 0, 0, _RET_ERRNO))
    return b"".join(program)


class _SockFprog(ctypes.Structure):
    _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.c_void_p))


class Filter:
    """The seccomp filter, built in the companion and installed by the child before `exec`."""

    def __init__(self) -> None:
        arch = machine()
        if arch is None:
            raise OSError(f"no seccomp filter for the architecture {platform.machine()!r}")
        program = build_program(arch)
        self._buffer = ctypes.create_string_buffer(program, len(program))  # kept alive
        self._fprog = _SockFprog(len(program) // 8, ctypes.cast(self._buffer, ctypes.c_void_p))

    def install(self) -> None:
        _prctl(_PR_SET_NO_NEW_PRIVS, 1)
        if _c().prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(self._fprog), 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "seccomp filter")


_SECCOMP_WORKS: bool | None = None


def seccomp_ready() -> bool:
    """Whether a child of this process can have the filter installed (tried once, in a child that
    only starts and stops: the filter is never installed in this process)."""
    global _SECCOMP_WORKS
    if _SECCOMP_WORKS is None:
        try:
            filt = Filter()
            done = subprocess.run(
                [sys.executable, "-I", "-c", "pass"],
                preexec_fn=filt.install,
                capture_output=True,
                check=False,
                timeout=30,
            )
            _SECCOMP_WORKS = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _SECCOMP_WORKS = False
    return _SECCOMP_WORKS


# -- the process tree --------------------------------------------------------------------------


def _table() -> dict[int, tuple[int, str]]:
    """Every process of this pid namespace: its parent and its state, from `/proc`."""
    table: dict[int, tuple[int, str]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            text = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8")
        except OSError:
            continue  # gone between the listing and the read
        fields = text.rpartition(")")[2].split()
        if len(fields) > 1:
            table[int(entry)] = (int(fields[1]), fields[0])
    return table


def children_of(pid: int) -> set[int]:
    """The direct children of `pid`: the kernel's own list when it has one (a few reads), else
    the whole table. It has to be quick: a child that forks again every few milliseconds is
    stopped from this list, and a scan of every process is slower than that."""
    try:
        found: set[int] = set()
        for task in os.listdir(f"/proc/{pid}/task"):
            listed = Path(f"/proc/{pid}/task/{task}/children").read_text(encoding="utf-8")
            found |= {int(p) for p in listed.split()}
        return found
    except (OSError, ValueError):
        return {p for p, (parent, _) in _table().items() if parent == pid}


def _below(root: int, table: dict[int, tuple[int, str]]) -> set[int]:
    """`root` and everything beneath it in `table`, which is read once: a process that forks all
    day leaves hundreds of zombies, and a table per pid would take minutes."""
    by_parent: dict[int, list[int]] = {}
    for pid, (parent, _) in table.items():
        by_parent.setdefault(parent, []).append(pid)
    found, frontier = {root}, [root]
    while frontier:
        for child in by_parent.get(frontier.pop(), ()):
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found


def descendants_of(pid: int) -> set[int]:
    return _below(pid, _table()) - {pid}


def orphans(keep: Iterable[int]) -> set[int]:
    """The children this process has that it did not start: whatever lost its parent and was
    adopted (`keep` is what it did start, or was running beside it already)."""
    return children_of(os.getpid()) - set(keep)


def kill_trees(roots: Iterable[int], *, rounds: int = 20) -> None:
    """Kill each of `roots` and everything beneath it. What is found is first **stopped** (a
    stopped process forks no more), the trees are looked at again until nothing new turns up, and
    only then is everything killed. The table is read once per round, however many roots."""
    wanted = list(roots)
    stopped: set[int] = set()
    for _ in range(rounds):
        table = _table()
        living: set[int] = set()
        for root in wanted:
            living |= {p for p in _below(root, table) if p in table and table[p][1] != "Z"}
        living -= stopped
        if not living:
            break
        for victim in living:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(victim, signal.SIGSTOP)
        stopped |= living
    for victim in stopped:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(victim, signal.SIGKILL)


def kill_tree(pid: int) -> None:
    kill_trees([pid])


def sweep(keep: Iterable[int], *, rounds: int = 20) -> None:
    """Kill what this process adopted and did not start (see `orphans`), and collect it.

    Each round **stops** what it finds, from the kernel's list of children and nothing slower (a
    stopped process forks no more, so the next round has fewer), then kills the trees beneath,
    then collects. What a killed orphan had started is adopted in turn, so it goes round again
    until there is nothing left."""
    kept = set(keep)
    mine = os.getpid()
    for _ in range(rounds):
        found = children_of(mine) - kept
        if not found:
            return
        for pid in found:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGSTOP)
        kill_trees(found)
        time.sleep(0.01)
        reap(found)


def collect_zombies(keep: Iterable[int]) -> int:
    """Collect the adopted children that have died (not `keep`: the plugins' own, which the event
    loop waits for). A plugin that forks and lets its parent exit makes them by the dozen a
    second, and each holds a pid until someone waits for it."""
    table = _table()
    mine = os.getpid()
    dead = {p for p, (parent, state) in table.items() if parent == mine and state == "Z"}
    dead -= set(keep)
    reap(dead)
    return len(dead)


def reap(pids: Iterable[int]) -> None:
    """Collect the children of ours that were killed: one that nobody waits for stays a zombie,
    and a zombie still answers `kill(pid, 0)`."""
    for pid in pids:
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, os.WNOHANG)
