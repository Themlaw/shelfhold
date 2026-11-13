"""Giving a Kavita library its own settings back at the full detachment (M3 L30, EF-EXP-031).

Adopting a library records what it was (`shelf_destination.pre_adoption_settings`: its type and
its `ExcludePatterns`) and adds one pattern, the one that hides `.shelfhold/` (EF-EXP-028). The
detachment takes that pattern away and leaves the library as the admin had it:

- the patterns of before are the patterns now, minus Shelfhold's own; a pattern the admin added
  since stays (it is theirs), one they removed since is not put back;
- a library Shelfhold created, never adopted, has no "before": only Shelfhold's pattern goes;
- the **type** is read back and compared with the one of before: Shelfhold never changes it and
  Kavita does not let an update change it, so a difference is told (it was changed by hand), not
  forced.

Idempotent: run again after an interruption, it finds the settings restored and writes nothing.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.dto import HIDDEN_DIR_PATTERNS, LibraryType
from shelfhold.export.targets.kavita.errors import ProtocolError

__all__ = [
    "LibraryGoneError",
    "SettingsRestored",
    "patterns_after_detach",
    "restore_library_settings",
]


class LibraryGoneError(ProtocolError):
    """The library is no longer in Kavita: there is nothing to give back."""


@dataclass(frozen=True)
class SettingsRestored:
    library_ref: str
    exclude_patterns_restored: bool  # something was written (False: already as it should be)
    patterns_now: tuple[str, ...]
    library_type_before: str | None
    library_type_now: str
    type_unchanged: bool  # True when no "before" was recorded: nothing to compare


def patterns_after_detach(current: tuple[str, ...], before: tuple[str, ...] | None) -> list[str]:
    """What `ExcludePatterns` becomes: Shelfhold's pattern goes unless the library had it before."""
    kept_from_before = set(before or ())
    return [p for p in current if p not in HIDDEN_DIR_PATTERNS or p in kept_from_before]


async def restore_library_settings(
    client: KavitaClient, library_ref: str, before: Mapping[str, Any] | None
) -> SettingsRestored:
    libraries = await client.list_libraries()
    library = next((lib for lib in libraries if str(lib.get("id")) == str(library_ref)), None)
    if library is None:
        raise LibraryGoneError(f"library {library_ref} is not in Kavita")
    recorded = None
    if before is not None and isinstance(before.get("exclude_patterns"), list):
        recorded = tuple(str(p) for p in before["exclude_patterns"])
    current = tuple(str(p) for p in library.get("excludePatterns") or ())
    wanted = patterns_after_detach(current, recorded)
    update = await client.replace_exclude_patterns(int(library_ref), wanted)
    type_now = LibraryType(int(library.get("type", 0))).kavita_name
    type_before = str(before["library_type"]) if before and before.get("library_type") else None
    return SettingsRestored(
        library_ref=str(library_ref),
        exclude_patterns_restored=update.changed,
        patterns_now=tuple(wanted),
        library_type_before=type_before,
        library_type_now=type_now,
        type_unchanged=type_before is None or type_before == type_now,
    )
