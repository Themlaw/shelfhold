"""Reading and writing `.shelfhold-mount-id` (EF-EXP-029, ENF-OPS-013).

The file is read through the confined root, so a sentinel that is a symbolic link is as good as
absent. It is rewritten atomically, keeping every field it already had.
"""

import os
from pathlib import Path

from shelfhold.export.domain.physical_roots import SENTINEL_FILE, SentinelDocument
from shelfhold.infrastructure.fsops.confined import ConfinedRoot, Strategy
from shelfhold.infrastructure.fsops.errors import PathEscapeError, SentinelFormatError
from shelfhold.infrastructure.fsops.writer import write_atomic

MAX_SENTINEL_BYTES = 64 * 1024


def read_sentinel(root: ConfinedRoot) -> SentinelDocument | None:
    """None when absent (or a link); `SentinelFormatError` when it cannot be understood."""
    try:
        fd = root.open_file(SENTINEL_FILE, os.O_RDONLY)
    except (FileNotFoundError, PathEscapeError):
        return None
    try:
        raw = os.read(fd, MAX_SENTINEL_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_SENTINEL_BYTES:
        raise SentinelFormatError("sentinel is too large")
    return SentinelDocument.parse(raw)


def write_sentinel(root: ConfinedRoot, doc: SentinelDocument) -> None:
    write_atomic(root, SENTINEL_FILE, doc.dumps(), file_mode=0o600)


def create_sentinel(
    path: str | Path, doc: SentinelDocument, *, strategy: Strategy = Strategy.AUTO
) -> None:
    """Stamp a root for the first time (or recreate a lost sentinel)."""
    with ConfinedRoot.open(path, strategy=strategy) as root:
        write_sentinel(root, doc)
