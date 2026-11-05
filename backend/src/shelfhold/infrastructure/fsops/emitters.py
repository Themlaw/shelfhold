"""Perimeters of the system emitters (passe 13, N2): where each may delete, checked at run time.

A system emitter is exempt from the reserved-action rule because no credential triggers it; in
exchange it acts only on paths the host itself created: the staging folder, probe files, the
trash. Nothing but this table widens that, and `test_fsops_emitter_perimeter` ties its keys to
the closed list `SYSTEM_EMITTERS` of the identity context (fsops cannot import it).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from shelfhold.export.domain.physical_roots import RECYCLE_DIR, STAGING_DIR, is_reserved
from shelfhold.infrastructure.fsops.errors import EmitterPerimeterError

PROBE_NAME = re.compile(r"\.shelfhold-probe-[A-Za-z0-9]+")


@dataclass(frozen=True)
class Perimeter:
    below: str | None = None  # a folder (relative to the root): only strictly under it
    name: re.Pattern[str] | None = None  # or a file name, outside the reserved area

    def allows(self, rel: str) -> bool:
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            return False
        if self.below is not None:
            prefix = self.below.split("/")
            return len(parts) > len(prefix) and parts[: len(prefix)] == prefix
        return (
            self.name is not None
            and not is_reserved(rel)
            and self.name.fullmatch(parts[-1]) is not None
        )

    def allows_content_of(self, rel: str) -> bool:
        """Emptying the folder `rel`: it is the perimeter folder itself or lies under it."""
        if self.below is None:
            return False
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        prefix = self.below.split("/")
        return ".." not in parts and parts[: len(prefix)] == prefix


EMITTER_PERIMETERS: Mapping[str, Perimeter] = MappingProxyType(
    {
        "staging_orphan_cleanup": Perimeter(below=STAGING_DIR),
        "probe_file_cleanup": Perimeter(name=PROBE_NAME),
        "trash_retention_purge": Perimeter(below=RECYCLE_DIR),
        "disk_guard_purge": Perimeter(below=RECYCLE_DIR),
    }
)


def perimeter_of(emitter: str) -> Perimeter:
    try:
        return EMITTER_PERIMETERS[emitter]
    except KeyError:
        raise EmitterPerimeterError(f"{emitter!r} is not a system emitter") from None


def require_inside(emitter: str, rel: str) -> None:
    if not perimeter_of(emitter).allows(rel):
        raise EmitterPerimeterError(f"{emitter} may not delete {rel!r}: outside its perimeter")


def require_content_inside(emitter: str, rel: str) -> None:
    if not perimeter_of(emitter).allows_content_of(rel):
        raise EmitterPerimeterError(f"{emitter} may not empty {rel!r}: outside its perimeter")
