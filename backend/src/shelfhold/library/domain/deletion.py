"""Three levels of deletion, and what each one announces (EF-BIB-021, EF-BIB-031).

1. `unfollow`: the Unit is no longer followed; no file moves.
2. `remove_destinations`: the files served in the destinations go to the bin; Shelfhold's own
   copies (`/library`, `/store`) stay.
3. `delete_content`: every managed copy goes to the bin for 30 days; the Unit is no longer
   followed, so that nothing fetches it again.

The **preview** is a plain value computed from what the database and the disk say, writing
nothing. Its `version` is a digest of everything it announces: the application recomputes the
plan under the locks and does only what a plan of that very version says (`plan.stale` when
anything moved). Plain data and pure rules: the reading is in `library.adapters.deletion_inventory`
and the doing in `export.application.deletion`.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Final

__all__ = [
    "BIN_DAYS",
    "DeletionLevel",
    "DeletionPlan",
    "FileImpact",
    "UnitImpact",
    "plan_version",
]

BIN_DAYS: Final = 30  # ENF-OPS-009


class DeletionLevel(StrEnum):
    UNFOLLOW = "unfollow"
    REMOVE_DESTINATIONS = "remove_destinations"
    DELETE_CONTENT = "delete_content"

    @property
    def moves_files(self) -> bool:
        return self is not DeletionLevel.UNFOLLOW


@dataclass(frozen=True)
class FileImpact:
    """One managed file the application will move into a bin."""

    place: str  # library | store | destination
    root_id: str
    rel_path: str  # relative to its physical root
    size: int | None
    file_hash: str | None
    export_item_id: str | None = None
    destination_id: str | None = None  # the destination whose record served it


@dataclass(frozen=True)
class UnitImpact:
    unit_id: str
    irreplaceable: bool
    independent_copies: int
    independent_copies_after: int  # what the registry would count once these files are binned
    files: tuple[FileImpact, ...] = ()
    untouched: tuple[str, ...] = ()  # codes: copies Shelfhold never writes (read-only roots)
    blocked: tuple[str, ...] = ()  # codes: why this Unit cannot be deleted at this level
    exports: int = 0  # export records that serve a file of the Unit
    progress: int = 0  # reading positions on the Unit's exports and contents
    needs_confirmation: bool = False  # the identifier must be typed (irreplaceable, level 3)


@dataclass(frozen=True)
class DeletionPlan:
    level: DeletionLevel
    units: tuple[UnitImpact, ...]
    folders: tuple[str, ...] = ()  # `<destination>/<series folder>` the application empties
    version: str = field(default="")

    @property
    def blocked(self) -> bool:
        return any(u.blocked for u in self.units)

    @property
    def files(self) -> int:
        return sum(len(u.files) for u in self.units)

    @property
    def bytes(self) -> int:
        return sum(f.size or 0 for u in self.units for f in u.files)

    @property
    def confirmations(self) -> tuple[str, ...]:
        return tuple(u.unit_id for u in self.units if u.needs_confirmation)


def plan_version(level: DeletionLevel, units: Sequence[UnitImpact], folders: Sequence[str]) -> str:
    """A digest of everything the preview announces, the flags and the counts included: a Unit
    that became irreplaceable since, a file that arrived, a folder that is no longer empty all
    change it."""
    material = json.dumps(
        {"level": level.value, "units": [asdict(u) for u in units], "folders": list(folders)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).digest()[:16].hex()
