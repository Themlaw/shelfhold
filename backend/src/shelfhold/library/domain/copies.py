"""What a purge names (a copy to erase, a hash a tombstone cites, EF-BIB-052) and what makes a kept
copy count (EF-BIB-027). Plain data."""

from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True)
class Copy:
    """A copy to erase: a file (`rel_path` under `root_path`) or a bin entry (`rel_path` is the
    entry folder under the root's `.shelfhold/recycle`)."""

    kind: str  # `file` | `bin_entry`
    row_id: str
    location: str  # library | destination | store | adopted_ro | trash
    root_id: str
    root_path: str
    root_mode: str  # rw | adopt_ro
    rel_path: str
    unit_id: str | None
    export_item_id: str | None


@dataclass(frozen=True)
class Hash:
    by: str
    value: str


@dataclass(frozen=True)
class CopyFacts:
    """What was seen of a file on disk: its inode (with the device) and the hash it was read at."""

    dev: int
    ino: int
    sha256: str  # `sha256:<hex>`, from the bytes, not from the manifest


def is_independent(
    copy: CopyFacts, *, stored_sha256: str | None, siblings: Collection[tuple[int, int]]
) -> bool:
    """Is `copy` a copy of its own, next to `siblings` (the `(dev, ino)` of the Unit's other
    files)? (EF-BIB-027, EF-BIB-045)

    The Health page counts "with a kept copy" only when both hold: an inode that none of the other
    copies has, and the hash that was stored, read again now. A reflink clone has its own inode and
    shares only blocks that are copied on write, so it counts; a hard link is one inode under two
    names, so it does not (it lives and dies with the other name). The hash is checked at the time
    of the look, never assumed from the way the copy was made."""
    return copy.sha256 == stored_sha256 and (copy.dev, copy.ino) not in siblings
