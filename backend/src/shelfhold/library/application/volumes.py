"""The `/data` file layer of the library's application: the one-file eraser of the derived
caches (EF-BIB-018, M6 L12).

The sweep of the caches erases through here and nowhere else: one regular file or one link,
never a folder (not even as a tree), never across a mount point. The closed allowlist of the
paths the host creates is checked by the caller (`cache_quotas`) before anything is opened;
what is left is walked with descriptors only.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

__all__ = ["unlink_data_file"]


def unlink_data_file(data_dir: Path, rel: str) -> bool:
    """Remove one file under `/data` — a regular file or a link, never a folder: what the sweep
    of the caches erases (EF-BIB-018). Returns whether something went; a path that is not there
    any more answers False (nothing to erase, not a refusal: the row that names it goes).

    `rel` is a plain relative path (the caller checked the allowlist). Every step of the walk
    is opened with `O_NOFOLLOW` (a link on the way is refused, not followed) and must sit on
    the same device as `/data` itself: a mount point inside the path — or at its end — is
    refused (`ValueError`), so the eraser never crosses into another volume. The last name
    must be a regular file or a link (`ValueError` otherwise): a folder is never removed, not
    even as a tree. An erasure that fails (`OSError`) is the caller's to log and skip: the
    sweep goes on."""
    parts = rel.split("/")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds = [os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        device = os.fstat(fds[0]).st_dev
        for part in parts[:-1]:  # dirfd walk, O_NOFOLLOW: a swap for a link cannot redirect it
            try:
                fds.append(os.open(part, flags, dir_fd=fds[-1]))
            except FileNotFoundError:
                return False  # a folder of the way is gone: nothing to erase, not a refusal
            except OSError as error:  # ELOOP / ENOTDIR: a link or a file on the way
                raise ValueError(f"{part!r} on the way to {rel!r} is not a plain folder") from error
            if os.fstat(fds[-1]).st_dev != device:
                raise ValueError(f"{rel!r} crosses a mount point at {part!r}")
        parent, name = fds[-1], parts[-1]
        try:
            found = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if found.st_dev != device:
            raise ValueError(f"{rel!r} sits on another volume than /data")
        if not (stat.S_ISREG(found.st_mode) or stat.S_ISLNK(found.st_mode)):
            raise ValueError(f"{rel!r} is not a file: a folder is never removed as a tree")
        try:
            os.unlink(name, dir_fd=parent)  # a link at the end is removed as a link
        except FileNotFoundError:
            return False  # gone between the look and the take: nothing to erase
        return True
    finally:
        for fd in fds:
            os.close(fd)
