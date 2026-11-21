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

Standard library only (`ctypes` for the three Landlock system calls, `/proc` for the process tree).
"""

import contextlib
import ctypes
import os
import signal
import struct
import time
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "Ruleset",
    "harden_process",
    "kill_tree",
    "landlock_abi",
    "orphans",
    "reap",
    "restore_process",
]

_SYS_CREATE, _SYS_ADD, _SYS_RESTRICT = 444, 445, 446  # the same number on x86-64 and arm64
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_DUMPABLE, _PR_GET_DUMPABLE = 4, 3
_PR_SET_CHILD_SUBREAPER, _PR_GET_CHILD_SUBREAPER = 36, 37
_PR_SET_NO_NEW_PRIVS = 38

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


def _syscall(number: int, *args: int | ctypes.c_void_p | None) -> int:
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
    return {p for p, (parent, _) in _table().items() if parent == pid}


def descendants_of(pid: int) -> set[int]:
    table = _table()
    found: set[int] = set()
    frontier = {pid}
    while frontier:
        frontier = {p for p, (parent, _) in table.items() if parent in frontier} - found
        found |= frontier
    return found


def orphans(keep: Iterable[int]) -> set[int]:
    """The children this process has that it did not start: whatever lost its parent and was
    adopted (`keep` is what it did start, or was running beside it already)."""
    return children_of(os.getpid()) - set(keep)


def kill_tree(pid: int, *, rounds: int = 20) -> None:
    """Kill `pid` and everything beneath it, again until nothing is left that could still fork."""
    for _ in range(rounds):
        table = _table()
        victims = descendants_of(pid) | {pid}
        living = [p for p in victims if p in table and table[p][1] != "Z"]
        if not living:
            return
        for victim in living:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(victim, signal.SIGKILL)
        time.sleep(0.01)


def reap(pids: Iterable[int]) -> None:
    """Collect the children of ours that were killed: one that nobody waits for stays a zombie,
    and a zombie still answers `kill(pid, 0)`."""
    for pid in pids:
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, os.WNOHANG)
