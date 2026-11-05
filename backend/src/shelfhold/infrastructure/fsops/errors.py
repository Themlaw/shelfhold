"""Errors of fsops. All are `OSError`s, so a caller that handles I/O failures handles these."""

import errno

from shelfhold.export.domain.physical_roots import RootState, SentinelFormatError

__all__ = [
    "CopyVerificationError",
    "CrossRootError",
    "EmitterConditionError",
    "EmitterPerimeterError",
    "FsopsError",
    "NotRegularFileError",
    "OutsidePathMapError",
    "PathEscapeError",
    "ReadOnlyRootError",
    "ReservedPathError",
    "RootBlockedError",
    "SentinelFormatError",
]


class FsopsError(OSError):
    """A refusal of fsops: the operation was not attempted, or was undone."""

    def __init__(self, message: str, *, code: int = 0) -> None:
        super().__init__(code, message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class PathEscapeError(FsopsError):
    """The path leaves its root or goes through a symbolic link (ENF-SEC-012)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=errno.ELOOP)


class NotRegularFileError(FsopsError):
    """A FIFO, a device, a socket or a directory where a file was expected: never opened."""


class ReservedPathError(FsopsError):
    """`.shelfhold-mount-id` and `.shelfhold/**` belong to the host alone (I37)."""


class OutsidePathMapError(FsopsError):
    """The path lies under no root, or under a root but in no entry of the path map."""


class ReadOnlyRootError(FsopsError):
    """An `adopt_ro` root is never a write target (EF-EXP-060, I24)."""


class CrossRootError(FsopsError):
    """A rename between two physical roots would be a copy: it is never a `rename` (I24)."""


class CopyVerificationError(FsopsError):
    """The copy did not read back as what was sent: nothing was put in place."""


class RootBlockedError(FsopsError):
    """The root's sentinel is missing, different, behind or ahead: writes, deletions, scans stop."""

    def __init__(self, root_path: str, state: RootState) -> None:
        super().__init__(f"root {root_path} blocked: sentinel {state.value}")
        self.root_path = root_path
        self.state = state


class LeaseRaceError(FsopsError):
    """The lease file changed between its reading and its replacement: someone else won."""


class EmitterPerimeterError(FsopsError):
    """A system emitter tried to delete outside its perimeter, or is not a system emitter (N2)."""


class EmitterConditionError(FsopsError):
    """A system emitter is inside its perimeter but its condition does not hold (30-day trash,
    disk guard actually triggered): nothing was deleted."""
