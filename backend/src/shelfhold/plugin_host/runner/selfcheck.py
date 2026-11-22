"""What the companion checks of its own sandbox, at start and at every greeting (D-P1, pass 13).

The companion is the one place where a plugin's process is cut off from the core, and Docker does
the cutting: another uid, no `/data`, no `/store`, no `/library`, no CLI socket, no network, a
read-only root. The companion does not trust that it was started that way: it looks, and refuses to
start (or to serve a greeting) when one of the six is not true. An observation is plain data, so
that what the check decides can be tested without a container.

Standard library only.
"""

import contextlib
import errno
import os
import socket
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from shelfhold.plugin_host.runner.confine import landlock_abi, machine, seccomp_ready

__all__ = [
    "DOCKER_SOCKETS",
    "FORBIDDEN_PATHS",
    "PLUGINS_ENTRIES",
    "Observation",
    "SelfCheckFailed",
    "accepts",
    "interfaces_of",
    "observe",
    "probe_outbound",
    "problems_of",
    "reachability",
    "self_check",
]

FORBIDDEN_PATHS: Final = ("/data", "/store", "/library", "/data/shelfhold.sock")
DOCKER_SOCKETS: Final = ("/var/run/docker.sock", "/run/docker.sock")  # the engine is the host's
PLUGINS_ENTRIES: Final = ("site-packages", "state.json")  # all that `/plugins` is meant to hold
_TRANSIENT: Final = ".staging-"  # what the core is installing, while it is
# Documentation address (RFC 5737), never routed: a connection that goes anywhere is a network.
OUTBOUND_PROBE: Final = ("192.0.2.1", 9)
_NO_ROUTE: Final = {
    errno.ENETUNREACH,
    errno.EHOSTUNREACH,
    errno.ECONNREFUSED,
    errno.EACCES,
    errno.EPERM,
    errno.EADDRNOTAVAIL,
    errno.EAFNOSUPPORT,
}


@dataclass(frozen=True)
class Observation:
    uid: int
    visible_paths: tuple[str, ...]  # of FORBIDDEN_PATHS: the ones this process can reach
    interfaces: tuple[str, ...]
    outbound_possible: bool
    root_read_only: bool
    landlock_abi: int | None = None  # None: not looked at (a test's description of a sandbox)
    stray_entries: tuple[str, ...] = ()  # of the plugins volume: what is not meant to be there
    seccomp_ready: bool | None = None  # None: not looked at; False: the filter cannot be installed


class SelfCheckFailed(Exception):
    """The sandbox is not the one the companion needs; `problems` says every way it is not."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def interfaces_of(listing: str | Path = "/proc/net/dev") -> tuple[str, ...]:
    """The network interfaces of this network namespace, from the kernel's own table."""
    lines = Path(listing).read_text(encoding="utf-8").splitlines()[2:]
    return tuple(line.split(":", 1)[0].strip() for line in lines if ":" in line)


def probe_outbound(target: tuple[str, int] = OUTBOUND_PROBE, *, timeout: float = 1.0) -> bool:
    """Whether a connection to `target` goes anywhere: it connects, or it is neither refused nor
    unroutable (a packet that leaves and is never answered is a network too). In a container
    with no network the answer is the kernel's, at once: no route."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(target)
    except TimeoutError:
        return True
    except OSError as exc:
        return exc.errno not in _NO_ROUTE
    finally:
        probe.close()
    return True


def _reachable(path: str) -> bool:
    """Present and usable. A path that exists but that this uid may neither read, write nor
    traverse is not one it sees: the companion may start beside it."""
    if not os.path.lexists(path):
        return False
    return any(os.access(path, mode) for mode in (os.R_OK, os.W_OK, os.X_OK))


def stray_entries(plugins_root: str | Path) -> tuple[str, ...]:
    """What the plugins volume holds besides the installed wheels and the core's state: a mount of
    the core's data folder (`./data:/plugins`) shows up here, whatever it is called."""
    try:
        names = os.listdir(plugins_root)
    except OSError:  # no such volume: the companion has no plugin to run, and nothing to see
        return ()
    return tuple(
        sorted(n for n in names if n not in PLUGINS_ENTRIES and not n.startswith(_TRANSIENT))
    )


def observe(
    *,
    paths: Sequence[str] = (*FORBIDDEN_PATHS, *DOCKER_SOCKETS),
    probe: tuple[str, int] = OUTBOUND_PROBE,
    proc_net_dev: str | Path = "/proc/net/dev",
    plugins_root: str | Path = "/plugins",
) -> Observation:
    """Look at this process's surroundings, as they are now."""
    try:
        interfaces = interfaces_of(proc_net_dev)
    except OSError:  # no table to read: not a network namespace we can vouch for
        interfaces = ("?",)
    return Observation(
        uid=os.getuid(),
        visible_paths=tuple(p for p in paths if _reachable(p)),
        interfaces=interfaces,
        outbound_possible=probe_outbound(probe),
        root_read_only=bool(os.statvfs("/").f_flag & os.ST_RDONLY),
        landlock_abi=landlock_abi(),
        stray_entries=stray_entries(plugins_root),
        seccomp_ready=seccomp_ready(),
    )


def problems_of(seen: Observation, *, core_uid: int | None = None) -> list[str]:
    """Every way `seen` is not a sandbox (an empty list when it is). `core_uid`, when known, is
    the uid the core says it has, from the greeting."""
    problems: list[str] = []
    if seen.uid == 0:
        problems.append("the companion runs as root")
    if core_uid is not None and seen.uid == core_uid:
        problems.append(f"its uid {seen.uid} is the core's: a plugin would share the core's uid")
    problems += [f"it can see {path}" for path in seen.visible_paths]
    stray = [name for name in seen.interfaces if name != "lo"]
    if stray:
        problems.append(f"it has a network interface other than lo: {', '.join(stray)}")
    if seen.outbound_possible:
        problems.append("an outbound connection is possible")
    if not seen.root_read_only:
        problems.append("its root filesystem is not read-only")
    if seen.landlock_abi is not None and seen.landlock_abi < 1:
        problems.append(
            "the kernel offers no Landlock (it is needed to keep a plugin from writing "
            "beside its own scratch folder): use a kernel 5.13 or later with Landlock enabled"
        )
    if seen.seccomp_ready is False:
        problems.append(
            "the seccomp filter that keeps a plugin from leaving its process group and from "
            f"changing modes cannot be installed here (architecture {machine() or 'unknown'}; "
            "x86_64 and aarch64 are known)"
        )
    if seen.stray_entries:
        strays = ", ".join(seen.stray_entries)
        problems.append(f"the plugins volume holds more than the installed plugins: {strays}")
    return problems


def self_check(seen: Observation | None = None, *, core_uid: int | None = None) -> Observation:
    """`seen` (the present state by default) if it is a sandbox, else `SelfCheckFailed`."""
    seen = seen if seen is not None else observe()
    problems = problems_of(seen, core_uid=core_uid)
    if problems:
        raise SelfCheckFailed(problems)
    return seen


def reachability(
    socket_path: str | Path, *, repair: bool = False, folder: bool = True
) -> list[str]:
    """Why the core (another uid) could not connect to the socket, as the modes stand: the socket
    must be a socket that others may read and write and, with `folder`, sit in a folder others
    may traverse. `repair` puts back the socket's `0666` (the companion made it, and its plugins
    are kept from changing a mode, so a drift means something else did it, and it is said); the
    folder's mode is the volume's, reported and never changed here."""
    path = Path(socket_path)
    problems: list[str] = []
    try:
        mode = path.lstat()
        parent = path.parent.stat()
    except OSError as exc:
        return [f"{path} cannot be looked at: {type(exc).__name__}"]
    if not stat.S_ISSOCK(mode.st_mode):
        return [f"{path} is not a socket"]
    if folder and not parent.st_mode & stat.S_IXOTH:
        problems.append(f"the folder {path.parent} is not traversable by others")
    if stat.S_IMODE(mode.st_mode) & 0o006 != 0o006:
        problems.append(f"the socket {path} is not readable and writable by others")
        if repair:
            with contextlib.suppress(OSError):
                path.chmod(0o666)
    return problems


def accepts(socket_path: str | Path, *, timeout: float = 3.0) -> list[str]:
    """Whether something accepts on the unix socket (a problem, or nothing)."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(str(socket_path))
    except OSError as exc:
        return [f"the socket does not accept: {type(exc).__name__}"]
    finally:
        probe.close()
    return []
