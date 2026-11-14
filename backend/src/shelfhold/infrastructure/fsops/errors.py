"""Errors of fsops. All are `OSError`s, so a caller that handles I/O failures handles these."""

import errno

from shelfhold.export.domain.physical_roots import RootState, SentinelFormatError

__all__ = [
    "CopyVerificationError",
    "CrossRootError",
    "EmitterConditionError",
    "EmitterPerimeterError",
    "FileChangedError",
    "FsopsError",
    "HashInterrupted",
    "JournalChangedError",
    "LinkUnavailableError",
    "NotRegularFileError",
    "OutsidePathMapError",
    "PathEscapeError",
    "ReadOnlyRootError",
    "ReservedPathError",
    "RootBlockedError",
    "SentinelFormatError",
    "TombstonePendingError",
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


class FileChangedError(FsopsError):
    """The file is not the one the caller meant to delete (its content or identity moved)."""


class ErasingNameTakenError(FsopsError):
    """A folder named like the erasing container is at the top of the root and no open intention
    of the detachment vouches for it: it may be the admin's, so it is left as it is."""


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


class TombstonePendingError(FsopsError):
    """A root that came back owes tombstone lines (a purge ran while it was away): nothing else
    is written there before they are (EF-BIB-052, GAP-12-14). Replay the tombstones."""

    def __init__(self, root_path: str) -> None:
        super().__init__(f"root {root_path} has tombstone lines to write first")
        self.root_path = root_path


class LeaseRaceError(FsopsError):
    """The lease file changed between its reading and its replacement: someone else won."""


class EmitterPerimeterError(FsopsError):
    """A system emitter tried to delete outside its perimeter, or is not a system emitter (N2)."""


class EmitterConditionError(FsopsError):
    """A system emitter is inside its perimeter but its condition does not hold (30-day trash,
    disk guard actually triggered): nothing was deleted."""


class JournalChangedError(FsopsError):
    """The journal is not the file the rewrite was built from: somebody wrote it meanwhile, so
    nothing is replaced (the caller reads it again)."""


class LinkUnavailableError(FsopsError):
    """The hard link of the option (EF-EXP-015) cannot be made, and nothing was placed: the caller
    falls back to an independent copy and says so. `reason` is one of `cross_mount` (another file
    system or another mount), `not_regular`, `shared_name` (the file already has a second name that
    is not ours) or `unsupported` (the file system refuses links)."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or f"no hard link: {reason}", code=errno.EXDEV)
        self.reason = reason


class HashInterrupted(Exception):
    """`sha256_of(..., should_stop=...)` was told to stop between two blocks. Not an `OSError`:
    a caller that tolerates unreadable files must not take a stop for one."""
