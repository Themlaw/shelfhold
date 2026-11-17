"""The parent side: read a backup in the bounded subprocess (EF-INT-008, ENF-SEC-002).

The model is the image worker's (`shelfhold.infrastructure.imaging.client.run_worker`): an empty
environment and working directory, a new session killed whole on timeout, limits on memory and CPU,
a wall-clock deadline and a cap on what may come back. Nothing the file says is read here.
"""

from __future__ import annotations

import json
import sys
from typing import Final

from shelfhold.infrastructure.imaging.client import WorkerLimits, run_worker
from shelfhold.infrastructure.imaging.errors import DECODE_FAILED, ImageRejected
from shelfhold.platform.import_tachibk.decode import (
    INVALID,
    UNAVAILABLE,
    Limits,
    RawBackup,
    TachibkRefusedError,
)

WORKER_MODULE: Final = "shelfhold.platform.import_tachibk.worker"
_MEMORY: Final = 1024 * 1024 * 1024  # the file, its decompressed form and what is read from it


def decode_in_sandbox(
    data: bytes, limits: Limits | None = None, *, worker: WorkerLimits | None = None
) -> RawBackup:
    """The backup in `data`, or a `TachibkRefusedError` (a refusal of the file, or a worker that
    could not run: `import.tachibk_unavailable`, never the file's fault)."""
    limits = limits or Limits()
    if len(data) > limits.max_compressed:  # refused before a process is started
        raise TachibkRefusedError("too_large")
    run = worker or WorkerLimits(memory_bytes=_MEMORY, cpu_seconds=30, wall_seconds=60)
    spec = {"memory_bytes": run.memory_bytes, "cpu_seconds": run.cpu_seconds,
            "netns": run.network_namespace}  # fmt: skip
    argv = [sys.executable, "-I", "-m", WORKER_MODULE, json.dumps(spec)]
    try:
        result, payload = run_worker(
            "decode_tachibk", {"limits": limits.to_dict()}, data, limits=run, argv=argv
        )
    except ImageRejected as failed:
        if failed.code == DECODE_FAILED or failed.reason == "reply too large":
            # out of time or CPU, or too much to say: the file is the cause
            raise TachibkRefusedError("too_expensive", INVALID) from None
        raise TachibkRefusedError("worker_failed", UNAVAILABLE) from None
    refused = result.get("refused")
    if refused is not None:
        code = result.get("code")
        raise TachibkRefusedError(
            str(refused)[:40], code if code in (INVALID, "import.tachibk_schema") else INVALID
        )
    try:
        return RawBackup.from_dict(json.loads(payload))
    except (ValueError, TypeError):
        raise TachibkRefusedError("worker_failed", UNAVAILABLE) from None
