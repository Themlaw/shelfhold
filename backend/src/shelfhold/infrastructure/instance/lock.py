"""The instance lock: an exclusive `flock` on `/data/.lock` (ENF-OPS-015).

The kernel releases it when the process dies, so a crash never leaves it stuck. It is also the
keyring lock (`secrets rotate` and `retire_key` take it): while an instance runs, no other process
rotates the keys (PLAN-M1a section 7). The migration lock is a separate file, `migrate.lock`.
"""

import errno
import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Self

from shelfhold.infrastructure.instance.errors import InstanceLockHeldError, InstanceLockLinkError

LOCK_NAME = ".lock"
_open_descriptors: set[int] = set()


def _close_in_child() -> None:
    """A forked child must not keep the flock alive after the parent is gone (the descriptor is
    inherited): it drops its copy, the parent's stays."""
    for descriptor in list(_open_descriptors):
        os.close(descriptor)
    _open_descriptors.clear()


os.register_at_fork(after_in_child=_close_in_child)


class InstanceLock:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / LOCK_NAME
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        try:
            descriptor = os.open(
                self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
            )
        except OSError as error:
            if error.errno == errno.ELOOP:  # a planted symbolic link: never followed
                raise InstanceLockLinkError(
                    f"{self.path} is a symbolic link: refusing to start in write mode"
                ) from error
            raise
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise InstanceLockHeldError(
                f"another instance holds {self.path}: refusing to start in write mode"
            ) from None
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        _open_descriptors.add(descriptor)

    def is_held(self) -> bool:
        return self._descriptor is not None

    def release(self) -> None:
        if self._descriptor is not None:
            _open_descriptors.discard(self._descriptor)
            os.close(self._descriptor)  # closing drops the flock
            self._descriptor = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def holder_is_alive(lock_path: Path) -> bool:
    """True when some process holds the flock of `lock_path` (an instance that is still there)."""
    try:
        descriptor = os.open(lock_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)
