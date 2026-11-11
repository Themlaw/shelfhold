"""Covers the decode worker produced, kept under `/data/covers` (ENF-SEC-003).

Content-addressed: the name is the digest of the re-encoded bytes, so the same image is one file.
Only bytes the worker re-encoded are ever given here. A data writer, like `volumes.py`: `/data` is
not a library root.
"""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path

from shelfhold.infrastructure.secrets.atomic import write_private

__all__ = ["CoverStore"]


class CoverStore:
    def __init__(self, data_dir: Path) -> None:
        self._folder = data_dir / "covers"

    def put(self, data: bytes) -> str:
        """The reference (`covers/<sha256>.webp`, relative to `/data`) of the stored image."""
        name = f"{hashlib.sha256(data).hexdigest()}.webp"
        self._folder.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            write_private(self._folder / name, data, overwrite=False)
        return f"covers/{name}"
