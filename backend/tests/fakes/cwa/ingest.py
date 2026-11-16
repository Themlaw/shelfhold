"""The way a drop-box ingester behaves, made here: it looks at a folder, takes every whole EPUB
it finds there, and **removes** it (the file disappears after ingestion). A name that starts with a
dot, or that is not an `.epub`, is a file still being written: it is left alone and counted as a
violation, because a destination that shows one has not dropped atomically.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Ingested:
    name: str
    data: bytes


@dataclass
class FakeIngest:
    folder: Path
    taken: list[Ingested] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def scan(self) -> list[Ingested]:
        """One look at the folder: whole EPUBs are taken and removed, the rest is a violation."""
        found: list[Ingested] = []
        for path in sorted(self.folder.iterdir()):
            if path.is_symlink() or not path.is_file():
                self.violations.append(f"not a regular file: {path.name}")
                continue
            if path.name.startswith(".") or path.suffix.lower() != ".epub":
                self.violations.append(f"a file that is not a finished drop: {path.name}")
                continue
            data = path.read_bytes()
            if not zipfile.is_zipfile(path) or zipfile.ZipFile(path).testzip() is not None:
                self.violations.append(f"a partial EPUB: {path.name}")
                continue
            path.unlink()  # ingested: the file is gone
            found.append(Ingested(path.name, data))
        self.taken.extend(found)
        return found
