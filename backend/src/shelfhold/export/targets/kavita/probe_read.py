"""Does Kavita read the shared folder? (EF-UX-002, the probe read by Kavita)

The probe file is written by Shelfhold (fsops, `infrastructure.fsops.probe.readable_probe`); here
Kavita is asked to scan the folder and then to list what it indexed: the probe is read when one
of the files of a series of that folder is the probe, by the path Kavita knows it under. Nothing
else of the library is read or written, and the answer holds no key and no response body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import TargetError

__all__ = ["kavita_mount_hint", "kavita_reads"]

ATTEMPTS = 5
PAUSE_S = 2.0  # Kavita scans in the background: a few looks, never an unbounded wait


def kavita_mount_hint(remote_folder: str) -> str:
    """The compose block that gives Kavita the folder it cannot read: the same host folder
    Shelfhold writes, at the path the target's path map names, read-only."""
    return f"services:\n  kavita:\n    volumes:\n      - <host path>:{remote_folder}:ro\n"


async def kavita_reads(
    client: KavitaClient,
    library_id: int,
    remote_folder: str,
    remote_probe: str,
    *,
    attempts: int | None = None,
    pause_s: float | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Scan `remote_folder`, then look for the probe among the files of its series.

    A Kavita that cannot be asked (or that refuses) is a Kavita that did not read the probe: the
    failure is `False`, the cause is the one the admin sees on the target itself."""
    attempts = ATTEMPTS if attempts is None else attempts
    pause_s = PAUSE_S if pause_s is None else pause_s
    try:
        await client.scan_folder(remote_folder)
        for attempt in range(attempts):
            if attempt:
                await sleep(pause_s)
            for series in await client.series_of_library(library_id):
                if series.lives_in(remote_folder) and remote_probe in await client.series_files(
                    series.id
                ):
                    return True
    except TargetError:
        return False
    return False
