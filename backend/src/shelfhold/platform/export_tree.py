"""The folder an export is written to: the single module of the export that opens it for writing.

The folder is the operator's own (a mounted disk, a backup share), never a library root, `/data` or
`/store`: the caller checks that (`ExportFolderError`). Nothing is ever deleted here, not even on
failure: a folder that already holds something is refused, and a half-written export is told apart
from a whole one by its `manifest.json`, which is written last. Files are created with `O_EXCL`
and `O_NOFOLLOW` (a link someone planted is never followed), owner-only (0600 files, 0700 folders):
an export carries every profile's reading history.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import BinaryIO

__all__ = ["ExportFolderError", "ExportTree"]

_FILE_MODE = 0o600
_DIR_MODE = 0o700
_CHUNK = 1 << 20


class ExportFolderError(Exception):
    """The folder cannot take an export; `reason` says why (`inside_root`, `not_empty`...)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


class ExportTree:
    """A new, private folder and the files written below it."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @classmethod
    def create(cls, folder: Path | str, *, forbidden: tuple[Path, ...] = ()) -> ExportTree:
        """`folder` is absent (its parent exists) or an empty folder. `forbidden`: the roots, the
        folder may not be one of them nor inside one (resolved: a link does not hide it)."""
        folder = Path(os.path.abspath(folder))
        if folder.is_symlink():
            raise ExportFolderError("is_link")
        real = folder.resolve()
        for banned in forbidden:
            if _inside(real, banned.resolve()):
                raise ExportFolderError("inside_root")
        if folder.exists():
            if not folder.is_dir():
                raise ExportFolderError("not_a_folder")
            if any(folder.iterdir()):
                raise ExportFolderError("not_empty")
        else:
            if not folder.parent.is_dir():
                raise ExportFolderError("no_parent")
            try:
                os.mkdir(folder, _DIR_MODE)
            except OSError as exc:
                raise ExportFolderError("not_writable") from exc
        os.chmod(folder, _DIR_MODE)
        return cls(folder)

    @property
    def root(self) -> Path:
        return self._root

    def _path_of(self, rel: str) -> Path:
        pure = PurePosixPath(rel)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts or "." in pure.parts:
            raise ValueError("an export path is relative and stays below the folder")
        return self._root.joinpath(*pure.parts)

    def _ensure_parent(self, target: Path) -> None:
        missing = []
        parent = target.parent
        while parent != self._root and not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for folder in reversed(missing):
            os.mkdir(folder, _DIR_MODE)

    def _open_new(self, rel: str) -> tuple[int, Path]:
        target = self._path_of(rel)
        self._ensure_parent(target)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(target, flags, _FILE_MODE)
        os.fchmod(descriptor, _FILE_MODE)  # the umask never loosens it
        return descriptor, target

    def put_bytes(self, rel: str, data: bytes) -> None:
        descriptor, _ = self._open_new(rel)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def put_stream(self, rel: str, source: BinaryIO) -> tuple[str, int]:
        """Stream `source` into a new file; returns its `sha256:<hex>` and its size."""
        descriptor, _ = self._open_new(rel)
        digest, size = hashlib.sha256(), 0
        try:
            while chunk := source.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return "sha256:" + digest.hexdigest(), size
