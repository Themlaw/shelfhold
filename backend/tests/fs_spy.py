"""A counter of the real accesses to the file system under some roots (ENF-OPS-014).

It wraps the `os` functions for the length of a `with` block and records every call whose path
(absolute, relative to a `dir_fd`, or an open descriptor) lies under a watched root. It changes
nothing of what the code under test does: each call goes on to the real function.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_PATH_FUNCTIONS = (
    "open", "stat", "lstat", "listdir", "scandir", "mkdir", "unlink", "remove", "rmdir",
    "rename", "replace", "utime", "access", "readlink", "symlink", "link", "truncate",
    "statvfs", "chmod", "fstat", "fsync",
)  # fmt: skip
_real_readlink = os.readlink


class FsSpy:
    def __init__(self, roots: Sequence[Path]) -> None:
        self._roots = tuple(os.path.realpath(r) for r in roots)
        self.accesses: list[str] = []
        self._inside = threading.local()  # the spy's own look-ups are not accesses

    def _under(self, path: str) -> bool:
        real = os.path.realpath(path)
        return any(real == r or real.startswith(r + os.sep) for r in self._roots)

    @staticmethod
    def _of_fd(fd: int) -> str | None:
        try:
            return _real_readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return None

    def _resolve(self, target: Any, dir_fd: int | None) -> str | None:
        if isinstance(target, int):
            return self._of_fd(target)
        if not isinstance(target, str | bytes | os.PathLike):
            return None
        text = os.fsdecode(os.fspath(target))
        if os.path.isabs(text) or dir_fd is None:
            return os.path.abspath(text)
        base = self._of_fd(dir_fd)
        return None if base is None else os.path.join(base, text)

    def _record(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        pairs = [(args[0] if args else kwargs.get("path"), kwargs.get("dir_fd"))]
        if name in ("rename", "replace", "link", "symlink"):
            second = args[1] if len(args) > 1 else kwargs.get("dst")
            pairs = [
                (args[0] if args else kwargs.get("src"), kwargs.get("src_dir_fd")),
                (second, kwargs.get("dst_dir_fd")),
            ]
        for target, dir_fd in pairs:
            found = self._resolve(target, dir_fd)
            if found is not None and self._under(found):
                self.accesses.append(f"{name} {found}")
                return

    @contextmanager
    def watching(self) -> Iterator[FsSpy]:
        saved: dict[str, Callable[..., Any]] = {}

        def wrap(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
            def spy(*args: Any, **kwargs: Any) -> Any:
                if not getattr(self._inside, "busy", False):
                    self._inside.busy = True
                    try:
                        self._record(name, args, kwargs)
                    finally:
                        self._inside.busy = False
                return real(*args, **kwargs)

            return spy

        for name in _PATH_FUNCTIONS:
            real = getattr(os, name, None)
            if real is not None:
                saved[name] = real
                setattr(os, name, wrap(name, real))
        try:
            yield self
        finally:
            for name, real in saved.items():
                setattr(os, name, real)
