# SPDX-License-Identifier: MIT
"""The kit's isolation check: what a plugin's own process can see (EF-USR-024, pass 13).

A plugin that runs isolated has another uid than the core, no `/data`, no `/store`, no `/library`,
no CLI socket, and an environment with nothing of Shelfhold's in it. Played in isolated mode, the
kit asks the process it runs in (`observe_sandbox`) and says which of that is not true. The same
module, run as a script, prints the observation as JSON: the host's companion plays it in a
plugin's subprocess and reads the answer.

Standard library only.
"""

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the kit imports this module lazily, the other way round would be a cycle
    from shelfhold_sdk.testing.kit import ConformanceReport

__all__ = ["SANDBOX_PATHS", "SandboxView", "observe_sandbox", "run_isolation_check"]

SANDBOX_PATHS = ("/data", "/store", "/library", "/data/shelfhold.sock")
_SECRETISH = re.compile(r"SECRET|PASSWORD|PASSPHRASE|TOKEN|KEY|CREDENTIAL|_FILE$", re.IGNORECASE)


@dataclass(frozen=True)
class SandboxView:
    """What one process sees of its surroundings: its uid, which of the paths a plugin must never
    reach it can reach, and the names (never the values) of its environment."""

    uid: int
    visible: tuple[str, ...]
    env_names: tuple[str, ...]


def _reachable(path: str) -> bool:
    """Present and usable: a path that exists but that the process may neither read, write nor
    traverse is not one it sees."""
    if not os.path.lexists(path):
        return False
    return any(os.access(path, mode) for mode in (os.R_OK, os.W_OK, os.X_OK))


def observe_sandbox(
    *, paths: Sequence[str] = SANDBOX_PATHS, environ: Mapping[str, str] | None = None
) -> SandboxView:
    """Observe the current process (`paths` and `environ` are what a test replaces)."""
    env = os.environ if environ is None else environ
    return SandboxView(
        uid=os.getuid(),
        visible=tuple(p for p in paths if _reachable(p)),
        env_names=tuple(sorted(env)),
    )


def run_isolation_check(
    view: SandboxView | None = None, *, core_uid: int | None = None
) -> "ConformanceReport":
    """The report of the four isolation checks for `view` (the current process by default).
    `core_uid` is the uid of the core, learnt from the host's handshake; without it the uid check
    cannot compare and is not played."""
    from shelfhold_sdk.testing.kit import ConformanceReport

    report = ConformanceReport()
    report.add_isolation(view if view is not None else observe_sandbox(), core_uid)
    return report


def add_isolation_checks(
    report: "ConformanceReport", view: SandboxView, core_uid: int | None
) -> None:
    sockets = [p for p in view.visible if p.endswith(".sock")]
    data = [p for p in view.visible if not p.endswith(".sock")]
    report.add("isolation_no_data", not data, f"the plugin sees {', '.join(data)}" if data else "")
    report.add(
        "isolation_no_cli_socket",
        not sockets,
        f"the plugin sees the CLI socket {', '.join(sockets)}" if sockets else "",
    )
    leaks = [n for n in view.env_names if n.startswith("SHELFHOLD_") or _SECRETISH.search(n)]
    report.add(
        "isolation_env_closed",
        not leaks,
        f"the environment holds {', '.join(leaks)}" if leaks else "",
    )
    if core_uid is not None:
        problems = []
        if view.uid == core_uid:
            problems.append(f"uid {view.uid} is the core's")
        if view.uid == 0:
            problems.append("the plugin runs as root")
        report.add("isolation_uid_distinct", not problems, "; ".join(problems))


def main() -> int:
    seen = observe_sandbox()
    json.dump(
        {"uid": seen.uid, "visible": list(seen.visible), "env_names": list(seen.env_names)},
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
