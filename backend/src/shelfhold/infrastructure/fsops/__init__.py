"""The only writer on the file system (openat2, atomic writes); see importlinter.ini.

Everything under /library goes through `Fsops`; `ConfinedRoot` is the descriptor-relative layer
below it. No other module opens a path for writing there (EF-EXP-060, ENF-SEC-012).
"""

from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.fsops.capabilities import Link, RootCapabilities
from shelfhold.infrastructure.fsops.confined import ConfinedRoot, Strategy
from shelfhold.infrastructure.fsops.emitters import EMITTER_PERIMETERS, new_probe_name
from shelfhold.infrastructure.fsops.errors import (
    CopyVerificationError,
    CrossRootError,
    EmitterConditionError,
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
    TombstonePendingError,
)
from shelfhold.infrastructure.fsops.facade import BootReport, Fsops, RootStatus
from shelfhold.infrastructure.fsops.identity import FileIdentity, identity_of
from shelfhold.infrastructure.fsops.openat2 import openat2_supported
from shelfhold.infrastructure.fsops.sentinel import create_sentinel, read_sentinel

__all__ = [
    "EMITTER_PERIMETERS",
    "BootReport",
    "ConfinedRoot",
    "CopyVerificationError",
    "CrossRootError",
    "EmitterConditionError",
    "EmitterPerimeterError",
    "FileIdentity",
    "Fsops",
    "FsopsError",
    "LeaseRaceError",
    "Link",
    "NotRegularFileError",
    "OutsidePathMapError",
    "PathEscapeError",
    "ReadOnlyRootError",
    "ReservedPathError",
    "RootBlockedError",
    "RootCapabilities",
    "RootState",
    "RootStatus",
    "SentinelFormatError",
    "Strategy",
    "TombstonePendingError",
    "create_sentinel",
    "identity_of",
    "new_probe_name",
    "openat2_supported",
    "read_sentinel",
]
