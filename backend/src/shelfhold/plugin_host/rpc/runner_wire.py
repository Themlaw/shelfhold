"""The greeting between the core and the plugin runner (D-P1, EF-SRC-016).

The core connects to the companion's socket and sends one frame, `runner.start`: which plugin
(`key`, and the `entry` point `module:attr` of an installed module), the core's own uid, and the
limits it asks for. The companion answers one frame, an `ack` naming its own uid or a refusal that
says why; from then on the connection is the plugin process's standard streams, byte for byte, and
what crosses is `rpc_protocol` 1. Nothing here names a path: the companion looks for the module
where it keeps the plugins, never where the core says.

Imports the standard library only: the companion runs without the rest of the host.
"""

import re
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "ENTRY",
    "KEY",
    "MAX_GREETING_BYTES",
    "RUNNER_REFUSED",
    "RUNNER_START",
    "Refusal",
    "StartRequest",
    "ack_message",
    "parse_start",
    "refusal_message",
    "start_message",
]

RUNNER_START: Final = "runner.start"
RUNNER_REFUSED: Final = -32030
MAX_GREETING_BYTES: Final = 4096  # a greeting is a few hundred bytes: more is not one

KEY: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
ENTRY: Final = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*"
)


class Refusal(Exception):
    """The companion does not serve this greeting; the message says why, for the core's log."""


@dataclass(frozen=True)
class StartRequest:
    number: int
    key: str
    entry: str
    core_uid: int
    memory_bytes: int
    cpu_seconds: int

    @property
    def module(self) -> str:
        return self.entry.partition(":")[0]


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def start_message(
    *, key: str, entry: str, core_uid: int, memory_bytes: int, cpu_seconds: int, number: int = 1
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": number,
        "method": RUNNER_START,
        "params": {
            "key": key,
            "entry": entry,
            "core_uid": core_uid,
            "memory_bytes": memory_bytes,
            "cpu_seconds": cpu_seconds,
        },
    }


def parse_start(message: Any) -> StartRequest:
    """The request, or `Refusal`. Nothing is trusted: names are matched whole, numbers are plain
    integers, and a field that is not there is a refusal, not a default."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise Refusal("not a JSON-RPC 2.0 request")
    number = _integer(message.get("id"))
    params = message.get("params")
    if message.get("method") != RUNNER_START or number is None or not isinstance(params, dict):
        raise Refusal(f"the first message is {RUNNER_START}, and nothing else")
    key, entry = params.get("key"), params.get("entry")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise Refusal("the plugin key is not valid")
    if not isinstance(entry, str) or not ENTRY.fullmatch(entry):
        raise Refusal("the entry point is not valid (module:attr)")
    core_uid = _integer(params.get("core_uid"))
    memory, cpu = _integer(params.get("memory_bytes")), _integer(params.get("cpu_seconds"))
    if core_uid is None or memory is None or cpu is None:
        raise Refusal("the core's uid and the limits are plain integers")
    return StartRequest(number, key, entry, core_uid, memory, cpu)


def ack_message(number: int, runner_uid: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": number, "result": {"runner_uid": runner_uid}}


def refusal_message(number: int, reason: str) -> dict[str, Any]:
    error = {"code": RUNNER_REFUSED, "message": f"refused: {reason}"[:500]}
    return {"jsonrpc": "2.0", "id": number, "error": error}
