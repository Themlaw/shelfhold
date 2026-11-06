"""File identity as recorded in `fs_intent.expected_file_id`: inode, size, mtime.

`st_dev` is deliberately absent: a device number changes when a disk comes back under another
name, while the inode, the size and the mtime say whether the file is the one that was expected.
Within one root and one boot, `(dev, ino)` is still compared by the caller that needs it.
"""

from dataclasses import dataclass
from typing import Protocol


class StatLike(Protocol):
    @property
    def st_ino(self) -> int: ...
    @property
    def st_size(self) -> int: ...
    @property
    def st_mtime_ns(self) -> int: ...


@dataclass(frozen=True)
class FileIdentity:
    ino: int
    size: int
    mtime_ns: int

    def to_json(self) -> dict[str, int]:
        return {"ino": self.ino, "size": self.size, "mtime_ns": self.mtime_ns}

    @classmethod
    def from_json(cls, data: dict[str, int]) -> "FileIdentity":
        return cls(int(data["ino"]), int(data["size"]), int(data["mtime_ns"]))


def identity_of(observed: StatLike) -> FileIdentity:
    return FileIdentity(observed.st_ino, observed.st_size, observed.st_mtime_ns)
