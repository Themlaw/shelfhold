"""The only writer on the file system (openat2, atomic writes); see importlinter.ini.

Everything under /library goes through `Fsops`; `ConfinedRoot` is the descriptor-relative layer
below it. No other module opens a path for writing there (EF-EXP-060, ENF-SEC-012).
"""

from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.fsops.confined import ConfinedRoot, Strategy
from shelfhold.infrastructure.fsops.emitters import EMITTER_PERIMETERS
from shelfhold.infrastructure.fsops.errors import (
    CopyVerificationError,
    CrossRootError,
    EmitterPerimeterError,
    FsopsError,
    LeaseRaceError,
    NotRegularFileError,
    OutsidePathMapError,
    PathEscapeError,
    ReadOnlyRootError,
    ReservedPathError,
    RootBlockedError,
    SentinelFormatError,
)
from shelfhold.infrastructure.fsops.facade import BootReport, Fsops, RootStatus
from shelfhold.infrastructure.fsops.identity import FileIdentity, identity_of, same_file
from shelfhold.infrastructure.fsops.openat2 import openat2_supported
from shelfhold.infrastructure.fsops.sentinel import create_sentinel, read_sentinel

__all__ = [
    "EMITTER_PERIMETERS",
    "BootReport",
    "ConfinedRoot",
    "CopyVerificationError",
    "CrossRootError",
    "EmitterPerimeterError",
    "FileIdentity",
    "Fsops",
    "FsopsError",
    "LeaseRaceError",
    "NotRegularFileError",
    "OutsidePathMapError",
    "PathEscapeError",
    "ReadOnlyRootError",
    "ReservedPathError",
    "RootBlockedError",
    "RootState",
    "RootStatus",
    "SentinelFormatError",
    "Strategy",
    "create_sentinel",
    "identity_of",
    "openat2_supported",
    "read_sentinel",
    "same_file",
]
