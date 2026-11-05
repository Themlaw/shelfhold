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


def sentinel_kids(doc: SentinelDocument | None) -> dict[str, dict[str, str]]:
    """The entries of the sentinel's registry: kid -> {signer kid: tag}. Whether a tag verifies
    is the caller's business (it holds the keys); a malformed entry or field is skipped, and an
    entry without tags (or of the first draft, a bare string) is listed with none."""
    if doc is None:
        return {}
    value = doc.extra.get(SENTINEL_KIDS_FIELD)
    if not isinstance(value, list):
        return {}
    entries: dict[str, dict[str, str]] = {}
    for item in value:
        if isinstance(item, str):
            entries.setdefault(item, {})
        elif isinstance(item, dict) and isinstance(item.get("kid"), str):
            tags = item.get("tags")
            clean = (
                {k: v for k, v in tags.items() if isinstance(k, str) and isinstance(v, str)}
                if isinstance(tags, dict)
                else {}
            )
            entries.setdefault(item["kid"], {}).update(clean)
    return entries


def _at_least(version: str, floor: str) -> str:
    def parts(text: str) -> tuple[int, int]:
        major, _, minor = text.partition(".")
        return int(major), int(minor)

    return version if parts(version) >= parts(floor) else floor


def add_sentinel_kid(root: ConfinedRoot, kid: str, tags: dict[str, str]) -> bool:
    """Add `kid` (with its tags) to the sentinel's registry, or the missing tags of an entry that
    is there; never removes an entry. True when the sentinel was rewritten."""
    doc = read_sentinel(root)
    if doc is None:
        raise SentinelFormatError("the root has no sentinel")
    if doc.read_only:
        raise SentinelFormatError("sentinel of a newer major format: read only")
    current = doc.extra.get(SENTINEL_KIDS_FIELD)
    items: list[object] = list(current) if isinstance(current, list) else []
    for position, item in enumerate(items):
        if isinstance(item, dict) and item.get("kid") == kid:
            found = item.get("tags")
            known: dict[str, object] = found if isinstance(found, dict) else {}
            merged = {**tags, **known}  # an existing tag is never replaced
            if merged == known:
                return False
            items[position] = {**item, "tags": merged}
            break
    else:
        items.append({"kid": kid, "tags": dict(tags)})
    extra = {**doc.extra, SENTINEL_KIDS_FIELD: items}
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


def sentinel_kid_names(path: str) -> frozenset[str] | None:
    """Read only: every kid the sentinel of the root at `path` names, tagged or not (used by
    `retire_key`, for which a forged entry can only keep a key). None when the root is not
    mounted, has no sentinel, or its sentinel or its registry cannot be read (fail closed)."""
    try:
        with ConfinedRoot.open(path) as root:
            doc = read_sentinel(root)
    except (OSError, SentinelFormatError, ValueError):
        return None
    if doc is None:
        return None
    value = doc.extra.get(SENTINEL_KIDS_FIELD, [])
    if not isinstance(value, list):
        return None
    names: set[str] = set()
    for item in value:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and isinstance(item.get("kid"), str):
            names.add(item["kid"])
        else:
            return None
    return frozenset(names)
