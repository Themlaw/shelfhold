"""The tombstone journal and the kid registry of a root, on disk (EF-BIB-053, EF-BIB-054).

Both live in the host area of the root (`.shelfhold/tombstones`, `.shelfhold-mount-id`), which no
destination plan can name (I37). The journal is append only: a line is added with one `write` on
an `O_APPEND` descriptor, then `fsync`; nothing here can rewrite or shorten it (a key rotation
never touches it, ENF-SEC-005). The file is created owner-only (0600) with its header line. The
registry of kids (`tombstone_kids` in the sentinel) only grows and keeps every unknown field.
"""

import os
import stat

from shelfhold.export.domain.physical_roots import (
    SENTINEL_KIDS_FIELD,
    TOMBSTONE_JOURNAL,
    SentinelDocument,
)
from shelfhold.infrastructure.fsops.confined import ConfinedRoot
from shelfhold.infrastructure.fsops.errors import SentinelFormatError
from shelfhold.infrastructure.fsops.sentinel import read_sentinel, write_sentinel

KIDS_MINOR_VERSION = "1.1"  # sentinel minor version that introduced `tombstone_kids`


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def append_line(root: ConfinedRoot, line: bytes, *, header: bytes) -> None:
    """Append one line (ending in a newline); the header first if the file is new."""
    if not line.endswith(b"\n") or b"\n" in line[:-1]:
        raise ValueError("a journal line is one line")
    root.mkdir(TOMBSTONE_JOURNAL.rpartition("/")[0], parents=True, exist_ok=True)
    fd = root.open_file(TOMBSTONE_JOURNAL, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        status = os.fstat(fd)
        if stat.S_IMODE(status.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        created = status.st_size == 0
        _write_all(fd, (header if created else b"") + line)
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        root.fsync_dir(TOMBSTONE_JOURNAL.rpartition("/")[0])


def read_journal(root: ConfinedRoot, max_bytes: int) -> bytes | None:
    """The whole journal, or None when there is none; at most `max_bytes + 1` bytes are read."""
    try:
        fd = root.open_file(TOMBSTONE_JOURNAL, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes and (chunk := os.read(fd, min(1 << 20, max_bytes + 1 - total))):
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def sentinel_kids(doc: SentinelDocument | None) -> frozenset[str]:
    """The kids the sentinel lists; a malformed field counts as none listed."""
    if doc is None:
        return frozenset()
    value = doc.extra.get(SENTINEL_KIDS_FIELD)
    if not isinstance(value, list) or not all(isinstance(k, str) for k in value):
        return frozenset()
    return frozenset(value)


def _at_least(version: str, floor: str) -> str:
    def parts(text: str) -> tuple[int, int]:
        major, _, minor = text.partition(".")
        return int(major), int(minor)

    return version if parts(version) >= parts(floor) else floor


def add_sentinel_kid(root: ConfinedRoot, kid: str) -> bool:
    """Add `kid` to the sentinel's registry; True when the sentinel was rewritten."""
    doc = read_sentinel(root)
    if doc is None:
        raise SentinelFormatError("the root has no sentinel")
    if doc.read_only:
        raise SentinelFormatError("sentinel of a newer major format: read only")
    current = doc.extra.get(SENTINEL_KIDS_FIELD)
    kids = list(current) if isinstance(current, list) else []
    if kid in kids:
        return False
    extra = {**doc.extra, SENTINEL_KIDS_FIELD: [*kids, kid]}
    write_sentinel(
        root,
        SentinelDocument(
            doc.root_id,
            doc.instance_id,
            doc.generation,
            _at_least(doc.schema_version, KIDS_MINOR_VERSION),
            extra,
        ),
    )
    return True
