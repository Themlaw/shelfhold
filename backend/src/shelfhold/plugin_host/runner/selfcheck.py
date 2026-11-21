"""What the companion checks of its own sandbox, at start and at every greeting (D-P1, pass 13).

The companion is the one place where a plugin's process is cut off from the core, and Docker does
the cutting: another uid, no `/data`, no `/store`, no `/library`, no CLI socket, no network, a
read-only root. The companion does not trust that it was started that way: it looks, and refuses to
start (or to serve a greeting) when one of the six is not true. An observation is plain data, so
that what the check decides can be tested without a container.

Standard library only.
"""

import errno
import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "FORBIDDEN_PATHS",
    "Observation",
    "SelfCheckFailed",
    "interfaces_of",
    "observe",
    "probe_outbound",
    "problems_of",
    "self_check",
]

FORBIDDEN_PATHS: Final = ("/data", "/store", "/library", "/data/shelfhold.sock")
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


def observe(
    *,
    paths: Sequence[str] = FORBIDDEN_PATHS,
    probe: tuple[str, int] = OUTBOUND_PROBE,
    proc_net_dev: str | Path = "/proc/net/dev",
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
    return problems


def self_check(seen: Observation | None = None, *, core_uid: int | None = None) -> Observation:
    """`seen` (the present state by default) if it is a sandbox, else `SelfCheckFailed`."""
    seen = seen if seen is not None else observe()
    problems = problems_of(seen, core_uid=core_uid)
    if problems:
        raise SelfCheckFailed(problems)
    return seen
