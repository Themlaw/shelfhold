"""What the companion does to the processes around it (D-P1, pass 13, review of L05).

The plugins run under the companion's own uid, in its container. Docker cuts them off from the
core; this module cuts them off from the companion and from each other's leftovers:

- **Landlock** (`Ruleset`): a plugin's process may write nowhere but a scratch folder of its own
  (no file, folder, link or socket is created, removed, renamed or truncated anywhere else: not on
  the socket folder, not on the plugins volume), reads only the roots it needs (`read_roots`: the
  interpreter and its libraries, the installed wheels, its own scratch folder; never another
  plugin's), and, where the kernel has ABI 4, may bind or connect over TCP nowhere. With ABI 6 it
  also may signal only the processes of its own domain, and connect to no abstract unix socket
  outside it. It needs no privilege. A kernel without Landlock, or with one older than ABI 3
  (`truncate` and `utime` are not handled before it, so a plugin could empty a file it may not
  write), is a sandbox the companion refuses to start in (`landlock_abi`, `MIN_ABI`).
- **The process itself** (`harden_process`): not dumpable (a plugin of the same uid cannot read its
  environment or attach to it) and the subreaper of every process that loses its parent, so that a
  plugin which double-forks and calls `setsid()` is still the companion's to kill (`kill_tree`,
  `orphans`).

- **A seccomp filter** (`Filter`), posed beside Landlock before the plugin runs: `setsid` and
  `setpgid` do nothing (a plugin stays in the group `killpg` kills, however fast it forks), and the
  `chmod`, `chown` and `setxattr` families fail with `EPERM` (Landlock does not handle them: a
  plugin that owns the socket's folder could make it unreachable for the core). The filter is
  built by hand, for the two architectures the companion knows (x86-64, arm64); on another one it
  does not start. Where Landlock has no signal scope (ABI < 6, kernels before 6.12) the same
  filter refuses `kill`, `tkill`, `tgkill`, `rt_sigqueueinfo`, `rt_tgsigqueueinfo` and
  `pidfd_send_signal` toward any process but the plugin itself and its own group (`signals`).

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
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "MIN_ABI",
    "Filter",
    "Ruleset",
    "children_of",
    "collect_zombies",
    "harden_process",
    "kill_tree",
    "kill_trees",
    "landlock_abi",
    "orphans",
    "read_roots",
    "reap",
    "restore_process",
    "seccomp_ready",
    "sweep",
    "unconfined_actions",
]

_SYS_CREATE, _SYS_ADD, _SYS_RESTRICT = 444, 445, 446  # the same number on x86-64 and arm64
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_DUMPABLE, _PR_GET_DUMPABLE = 4, 3
_PR_SET_CHILD_SUBREAPER, _PR_GET_CHILD_SUBREAPER = 36, 37
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER = 22, 2

# access rights of the filesystem, by the ABI that introduced them
_WRITE_FILE, _READ_FILE, _READ_DIR = 1 << 1, 1 << 2, 1 << 3
_REMOVE_DIR, _REMOVE_FILE = 1 << 4, 1 << 5
_MAKES = sum(1 << n for n in range(6, 13))  # char, dir, regular, socket, fifo, block, symlink
_REFER, _TRUNCATE = 1 << 13, 1 << 14
_NET_BIND_TCP, _NET_CONNECT_TCP = 1 << 0, 1 << 1
_SCOPE_ABSTRACT_UNIX_SOCKET, _SCOPE_SIGNAL = 1 << 0, 1 << 1  # ABI 6
# The oldest ABI the companion starts on. Before 3 `truncate` (and with it `utime`, which no ABI
# handles) is not a right Landlock can refuse: a plugin could empty a file beside its scratch
# folder, that of another plugin included. The other way (refusing `truncate` in seccomp) would
# break `ftruncate` on the plugin's own files (SQLite, a download resumed): a kernel from 6.2 on is
# the smaller price.
MIN_ABI = 3

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


# what a plugin may read besides its scratch folder: the system, as the image has it
_SYSTEM_READS = ("/usr", "/lib", "/lib32", "/lib64", "/libx32", "/etc/ssl", "/etc/ld.so.cache")
_TIME_ZONE = ("/etc/localtime", "/usr/share/zoneinfo")  # (the second is under /usr: kept for a
# image whose /usr is not where the zone files are)
_DEVICES = ("/dev/null", "/dev/urandom")


def read_roots(*extra: Path) -> list[Path]:
    """The folders and files a plugin's process may read (never another plugin's scratch folder):
    the system (`/usr`, `/lib*`, `/etc/ssl`, the time zone, `/dev/null`, `/dev/urandom`), the
    interpreter and the virtual environment it runs from, and `extra` (the installed wheels). A
    folder of `sys.path` is a root too (an editable install is not under the prefix), unless it
    holds the temporary folder: the scratch folders are there."""
    temporary = Path(tempfile.gettempdir()).resolve()
    roots = [
        *map(Path, _SYSTEM_READS),
        *map(Path, _TIME_ZONE),
        *map(Path, _DEVICES),
        *(Path(p) for p in (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)),
        *extra,
    ]
    for entry in sys.path:
        if not entry:
            continue
        found = Path(entry).resolve()
        if found != temporary and found not in temporary.parents:
            roots.append(found)
    seen: dict[Path, None] = {}
    for root in roots:
        if root.exists():
            seen[root] = None
    return list(seen)


def _attribute(abi: int, handled: int, net: int, scoped: int) -> bytes:
    """`struct landlock_ruleset_attr` as the kernel of this ABI reads it: the net rights came with
    ABI 4, the scopes with ABI 6 (a longer structure than the kernel knows is refused)."""
    if abi >= 6:
        return struct.pack("QQQ", handled, net, scoped)
    if abi >= 4:
        return struct.pack("QQ", handled, net)
    return struct.pack("Q", handled)


class Ruleset:
    """A Landlock ruleset built by the companion, applied by the child before it runs the plugin.

    Everything that writes is handled, and only `writable` (folders) are granted it, beneath them;
    `/dev/null` may be written to. When `readable` is given (the roots of `read_roots`), reading is
    handled too: the plugin reads those roots and its `writable` folders, and nothing else (the
    scratch folder of another plugin, the companion's own files). Executing is not handled. With
    ABI 4 the TCP rights are handled and granted nowhere; with ABI 6 signals and abstract unix
    sockets are scoped to the plugin's own domain."""

    def __init__(self, writable: Iterable[Path], readable: Iterable[Path] | None = None) -> None:
        self.abi = landlock_abi()
        if self.abi < 1:
            raise OSError("the kernel offers no Landlock")
        if self.abi < MIN_ABI:
            raise OSError(
                f"Landlock ABI {self.abi} cannot refuse `truncate`: ABI {MIN_ABI} (Linux 6.2) "
                "or later is needed"
            )
        handled = _handled_fs(self.abi)
        reads = readable is not None
        if reads:
            handled |= _READ_FILE | _READ_DIR
        net = (_NET_BIND_TCP | _NET_CONNECT_TCP) if self.abi >= 4 else 0
        scoped = (_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL) if self.abi >= 6 else 0
        attr = _attribute(self.abi, handled, net, scoped)
        self._fd = _syscall(_SYS_CREATE, ctypes.c_char_p(attr), len(attr), 0)
        if self._fd < 0:
            raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
        try:
            for folder in writable:
                self._allow(folder, handled)
            null = Path(os.devnull)
            if null.exists():
                self._allow(null, _WRITE_FILE | _TRUNCATE | (_READ_FILE if reads else 0))
            for root in readable or ():
                self._allow(root, _READ_FILE | (_READ_DIR if root.is_dir() else 0))
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
# What signals a process to another. Where Landlock scopes signals (ABI 6) none of this is needed;
# elsewhere the filter lets a plugin signal itself and its own group (`kill` of its pid, of 0, of
# minus its pid; `tgkill` of its pid) and nothing else: not the companion, not another plugin, not
# `-1` (everyone), not a child it started by its pid (its group, `killpg`, reaches them), and no
# `pidfd_send_signal` at all (a descriptor says nothing of whom it signals) nor `tkill` (a bare
# thread id: `tgkill` is what the C library uses).
_SIGNALS: dict[str, dict[str, int]] = {
    "x86_64": {"kill": 62, "tkill": 200, "tgkill": 234, "rt_sigqueueinfo": 129,
               "rt_tgsigqueueinfo": 297, "pidfd_send_signal": 424},
    "aarch64": {"kill": 129, "tkill": 130, "tgkill": 131, "rt_sigqueueinfo": 138,
                "rt_tgsigqueueinfo": 240, "pidfd_send_signal": 424},
}  # fmt: skip
# The kernel signals on a plugin's behalf too: a descriptor whose owner is set (`F_SETOWN`,
# `F_SETOWN_EX`, `FIOSETOWN`, `SIOCSPGRP`) gets `SIGIO` (or `F_SETSIG`'s signal) sent to that owner,
# checked on the uid only. These commands do nothing and return 0, as `setsid` does (an `EPERM`
# would crash a library that only meant to own its own descriptor); the owner stays unset.
_OWNERS: dict[str, dict[str, int]] = {
    "x86_64": {"fcntl": 72, "ioctl": 16},
    "aarch64": {"fcntl": 25, "ioctl": 29},
}
_OWNER_COMMANDS = {"fcntl": (8, 15), "ioctl": (0x8901, 0x8902)}  # F_SETOWN(_EX); FIOSETOWN, SIOCSPGRP
_ARG1 = 24  # seccomp_data.args[1], its low word
_SIGNAL_NEVER = ("tkill", "pidfd_send_signal")
_SIGNAL_OR_GROUP = ("kill",)  # the others take a process id (a group id only for `kill`)
_BPF_LD_W_ABS, _BPF_JEQ_K, _BPF_JGE_K, _BPF_RET_K = 0x20, 0x15, 0x35, 0x06
_ARG0 = 16  # seccomp_data.args[0], its low word (both architectures are little-endian)


def _instruction(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k)


def machine() -> str | None:
    """The architecture the filter knows, or None (the companion does not start there)."""
    name = platform.machine()
    return name if name in _AUDIT_ARCH else None


def build_program(arch: str, own_pid: int | None = None) -> bytes:
    """The BPF program: another architecture kills the process (a 32-bit call would dodge the
    numbers below), the changes of mode and owner fail with `EPERM`, `setsid` and `setpgid`
    return 0 without doing anything, everything else is allowed. With `own_pid` (the plugin's
    process, which is its own group's leader) the signals to anything but it and its group fail
    with `EPERM` too (`_SIGNALS`)."""
    refused = [*sorted(_DENIED[arch].values())]
    stays = sorted(_STAYS[arch].values())
    signals = _SIGNALS[arch]
    # (code, jump if true, jump if false, k); a jump is the name of a label or a count
    code: list[tuple[int, str | int, str | int, int]] = [
        (_BPF_LD_W_ABS, 0, 0, 4),  # seccomp_data.arch
        (_BPF_JEQ_K, 1, 0, _AUDIT_ARCH[arch]),
        (_BPF_RET_K, 0, 0, _RET_KILL_PROCESS),
        (_BPF_LD_W_ABS, 0, 0, 0),  # seccomp_data.nr
    ]
    labels: dict[str, int] = {}
    if arch == "x86_64":
        code.append((_BPF_JGE_K, "eperm", 0, _X32))  # the x32 numbers are all refused
    code += [(_BPF_JEQ_K, "eperm", 0, n) for n in refused]
    code += [(_BPF_JEQ_K, "zero", 0, n) for n in stays]
    if own_pid is not None:
        for name in _SIGNAL_NEVER:
            code.append((_BPF_JEQ_K, "eperm", 0, signals[name]))
        for name in ("kill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo"):
            # the call's first argument is a process id; the accumulator is left as `nr` for the
            # checks that follow when it is not this call
            label = f"after_{name}"
            code.append((_BPF_JEQ_K, 0, label, signals[name]))
            code.append((_BPF_LD_W_ABS, 0, 0, _ARG0))
            code.append((_BPF_JEQ_K, "allow", 0, own_pid))
            if name in _SIGNAL_OR_GROUP:
                code.append((_BPF_JEQ_K, "allow", 0, 0))
                code.append((_BPF_JEQ_K, "allow", 0, (-own_pid) & 0xFFFFFFFF))
            code.append((_BPF_RET_K, 0, 0, _RET_ERRNO | errno.EPERM))
            labels[label] = len(code)
        for name, commands in _OWNER_COMMANDS.items():
            label = f"after_{name}"
            code.append((_BPF_JEQ_K, 0, label, _OWNERS[arch][name]))
            code.append((_BPF_LD_W_ABS, 0, 0, _ARG1))
            code += [(_BPF_JEQ_K, "zero", 0, command) for command in commands]
            code.append((_BPF_RET_K, 0, 0, _RET_ALLOW))  # any other command of the call
            labels[label] = len(code)
    code.append((_BPF_RET_K, 0, 0, _RET_ALLOW))
    labels["allow"] = len(code) - 1
    code.append((_BPF_RET_K, 0, 0, _RET_ERRNO | errno.EPERM))
    labels["eperm"] = len(code) - 1
    code.append((_BPF_RET_K, 0, 0, _RET_ERRNO))
    labels["zero"] = len(code) - 1

    def offset(jump: str | int, here: int) -> int:
        return labels[jump] - here - 1 if isinstance(jump, str) else jump

    return b"".join(
        _instruction(op, offset(jt, index), offset(jf, index), k)
        for index, (op, jt, jf, k) in enumerate(code)
    )


class _SockFprog(ctypes.Structure):
    _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.c_void_p))


class Filter:
    """The seccomp filter, built in the companion and installed by the child before `exec`.

    `signals` (by default: where Landlock cannot scope them, ABI < 6) adds the refusal of the
    signals to any process but the plugin and its group; the filter then needs the plugin's own
    pid, which the child knows, so it is built in the child, at `install`."""

    def __init__(self, *, signals: bool | None = None) -> None:
        arch = machine()
        if arch is None:
            raise OSError(f"no seccomp filter for the architecture {platform.machine()!r}")
        self._arch = arch
        self._signals = landlock_abi() < 6 if signals is None else signals
        self._buffer = ctypes.create_string_buffer(0)  # kept alive
        self._fprog = _SockFprog(0, None)
        self._build(None)  # a program that cannot be built is known here, not in the child

    def _build(self, own_pid: int | None) -> None:
        program = build_program(self._arch, own_pid)
        self._buffer = ctypes.create_string_buffer(program, len(program))
        self._fprog = _SockFprog(len(program) // 8, ctypes.cast(self._buffer, ctypes.c_void_p))

    def install(self) -> None:
        if self._signals:
            self._build(os.getpid())
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


_PROBE = """
import os, signal, sys
import fcntl
secret, scratch, fallback = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
out = []
def tried(name, action):
    try:
        action()
        out.append(name)
    except OSError:
        pass
tried("signal the companion", lambda: os.kill(os.getppid(), 0))
def own():  # the filter makes this a no-op: the owner is still unset (0)
    r, _w = os.pipe()
    fcntl.fcntl(r, fcntl.F_SETOWN, os.getppid())
    if fcntl.fcntl(r, fcntl.F_GETOWN) != os.getppid():
        raise OSError
if fallback:  # where Landlock scopes the signals, an owner is harmless: it is not delivered
    tried("make the companion the owner of a descriptor", own)
tried("read another's file", lambda: open(secret, "rb").read(1))
tried("list another's folder", lambda: os.listdir(os.path.dirname(secret)))
tried("empty another's file", lambda: os.truncate(secret, 0))
print(",".join(out))
"""
_UNCONFINED: tuple[str, ...] | None = None


def unconfined_actions() -> tuple[str, ...]:
    """What a plugin's process, bound the way the companion binds it, can still do that it must
    not (empty when the confinement holds): signal this process, read or list a folder that is not
    its own, empty a file it may not write. Tried once, in a child that does nothing else."""
    global _UNCONFINED
    if _UNCONFINED is None:
        try:
            with (
                tempfile.TemporaryDirectory(prefix="plugin-probe-") as other,
                tempfile.TemporaryDirectory(prefix="plugin-probe-") as scratch,
            ):
                handle, name = tempfile.mkstemp(dir=other)  # empty: opening it is what is tried
                os.close(handle)
                secret = Path(name)
                rules = Ruleset([Path(scratch)], read_roots())
                filt = Filter()

                def bind() -> None:  # pragma: no cover - runs in the child, before exec
                    rules.restrict()
                    filt.install()

                try:
                    done = subprocess.run(  # noqa: S603
                        [sys.executable, "-I", "-c", _PROBE, str(secret), scratch, "1" if filt._signals else "0"],
                        preexec_fn=bind,
                        start_new_session=True,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                finally:
                    rules.close()
                if done.returncode != 0:
                    _UNCONFINED = ("start a bound process",)
                else:
                    _UNCONFINED = tuple(n for n in done.stdout.strip().split(",") if n)
        except (OSError, subprocess.SubprocessError):
            _UNCONFINED = ("start a bound process",)
    return _UNCONFINED


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
