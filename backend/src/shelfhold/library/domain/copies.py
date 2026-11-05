"""What a purge names: a copy to erase, a hash a tombstone cites (EF-BIB-052). Plain data."""

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
