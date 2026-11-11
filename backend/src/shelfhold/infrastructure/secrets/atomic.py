"""Owner-only files written atomically (temporary file, fsync, rename, fsync of the folder).

Used for the keyring and for the export file. A reader sees the old file or the new one, never
half of one. The mode is set with `fchmod` so the umask cannot loosen it. `/data` is not a
confined library root, so this is the same technique as `migrate.py`, not `fsops`.
"""

import contextlib
import os
import secrets as token_source
from pathlib import Path

PRIVATE_MODE = 0o600


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private(path: Path, data: bytes, *, overwrite: bool) -> None:
    """Write `data` to `path` with mode 0600.

    `overwrite=False` never replaces an existing file (hard link, atomic and race-free) and
    raises `FileExistsError`. On any failure nothing is left behind at `path` or beside it.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{token_source.token_hex(4)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        PRIVATE_MODE,
    )
    try:
        try:
            os.fchmod(descriptor, PRIVATE_MODE)
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # fails with FileExistsError instead of clobbering
            os.unlink(temporary)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    _fsync_directory(path.parent)


def ensure_private_dir(path: Path) -> None:
    """Create `path` (and parents) for files this module writes; the mode of the folder is the
    umask's, the files in it are owner-only."""
    path.mkdir(parents=True, exist_ok=True)
