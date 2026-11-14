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

from collections.abc import Mapping, Sequence
from typing import Any

from shelfhold.export.application.library_settings import LibraryGoneError, SettingsRestored
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.dto import HIDDEN_DIR_PATTERNS, LibraryType

__all__ = [
    "LibraryGoneError",
    "SettingsRestored",
    "patterns_after_detach",
    "restore_library_settings",
]


def patterns_after_detach(current: tuple[str, ...], before: tuple[str, ...] | None) -> list[str]:
    """What `ExcludePatterns` becomes: Shelfhold's pattern goes unless the library had it before."""
    kept_from_before = set(before or ())
    return [p for p in current if p not in HIDDEN_DIR_PATTERNS or p in kept_from_before]


def _same_type(before: str, now: LibraryType) -> bool:
    try:
        return LibraryType.from_name(before) is now
    except ValueError:
        return before.casefold() in (now.kavita_name.casefold(), now.name.casefold())


async def restore_library_settings(
    client: KavitaClient, library_ref: str, prior_settings: Sequence[Mapping[str, Any] | None]
) -> SettingsRestored:
    """One library, every destination that was adopted on it: the patterns of before of all of
    them are kept, the type of each is compared. Raises `LibraryGoneError` when Kavita no longer
    has the library."""
    libraries = await client.list_libraries()
    library = next((lib for lib in libraries if str(lib.get("id")) == str(library_ref)), None)
    if library is None:
        raise LibraryGoneError(f"library {library_ref} is not in Kavita")
    lists = [
        b["exclude_patterns"]
        for b in prior_settings
        if b is not None and isinstance(b.get("exclude_patterns"), list)
    ]
    recorded = (
        tuple(dict.fromkeys(str(p) for patterns in lists for p in patterns)) if lists else None
    )
    current = tuple(str(p) for p in library.get("excludePatterns") or ())
    wanted = patterns_after_detach(current, recorded)
    update = await client.replace_exclude_patterns(int(library_ref), wanted)
    type_now = LibraryType(int(library.get("type", 0)))
    types = [
        str(b["library_type"]) for b in prior_settings if b is not None and b.get("library_type")
    ]
    changed = [t for t in types if not _same_type(t, type_now)]
    return SettingsRestored(
        library_ref=str(library_ref),
        exclude_patterns_restored=update.changed,
        patterns_now=tuple(wanted),
        library_type_before=(changed + types)[0] if (changed or types) else None,
        library_type_now=type_now.kavita_name,
        type_unchanged=not changed,
    )
