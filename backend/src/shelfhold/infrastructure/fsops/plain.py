"""A read by name for a file the host does not confine: opened without following a link or
waiting for a writer, and a regular file or nothing (a FIFO, a device, a link are refused)."""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from typing import BinaryIO

from shelfhold.infrastructure.fsops.errors import NotRegularFileError


@contextmanager
def open_regular(path: str) -> Iterator[BinaryIO]:
    """`O_NOFOLLOW | O_NONBLOCK`, then `fstat` on the open descriptor: the file that is read is the
    one that was checked, whatever happens to its name afterwards."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFileError(f"{path!r} is not a regular file")
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as stream:
        yield stream
