"""Shapes exchanged with Kavita, after the `openapi.json` frozen in the test fixtures."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Final

from shelfhold.export.targets.kavita.errors import ProtocolError

# Hidden folder of every physical root (EF-EXP-059): Kavita must never index it.
HIDDEN_DIR_PATTERNS: Final = ("**/.shelfhold/**",)


class LibraryType(IntEnum):
    """`LibraryType` of Kavita; the integer is what travels (`libraryType`, `type`)."""

    MANGA = 0
    COMIC = 1
    BOOK = 2
    IMAGE = 3
    LIGHT_NOVEL = 4
    COMIC_VINE = 5

    @property
    def kavita_name(self) -> str:
        return _NAMES[self]

    @classmethod
    def from_name(cls, name: str) -> LibraryType:
        for member, kavita_name in _NAMES.items():
            if name in (kavita_name, member.name):
                return member
        raise ValueError(f"unknown Kavita library type {name!r}")


_NAMES: Final = {
    LibraryType.MANGA: "Manga",
    LibraryType.COMIC: "Comic",
    LibraryType.BOOK: "Book",
    LibraryType.IMAGE: "Image",
    LibraryType.LIGHT_NOVEL: "LightNovel",
    LibraryType.COMIC_VINE: "ComicVine",
}


@dataclass(frozen=True)
class ParseResult:
    """What `plugin/parse` read in a file name (the name only; the shelving is Kavita's)."""

    series_name: str
    series_year: str | None
    min_chapter: float
    max_chapter: float
    min_volume: float
    max_volume: float

    @classmethod
    def from_json(cls, data: object) -> ParseResult:
        if not isinstance(data, dict):
            raise ProtocolError("plugin/parse did not answer an object")
        return cls(
            series_name=str(data.get("seriesName") or ""),
            series_year=data.get("seriesYear"),
            min_chapter=float(data.get("minChapterNumber") or 0),
            max_chapter=float(data.get("maxChapterNumber") or 0),
            min_volume=float(data.get("minVolumeNumber") or 0),
            max_volume=float(data.get("maxVolumeNumber") or 0),
        )


@dataclass(frozen=True)
class LibraryUpdate:
    library_id: int
    changed: bool
    previous_exclude_patterns: tuple[str, ...]  # kept to restore them at detach (EF-EXP-031)


# LibraryDto (read) and UpdateLibraryDto (write) name these the same way.
_SAME: Final = (
    "id", "name", "type", "folders", "folderWatching", "includeInDashboard", "includeInSearch",
    "manageCollections", "manageReadingLists", "allowScrobbling", "allowMetadataMatching",
    "enableMetadata", "removePrefixForSortName", "inheritWebLinksFromFirstChapter",
)  # fmt: skip
_OPTIONAL: Final = ("defaultLanguage", "metadataProvider")
_ARCHIVE, _EPUB, _PDF = 1, 2, 3  # FileTypeGroup


def merged_patterns(existing: Iterable[str], wanted: Sequence[str]) -> list[str]:
    """Existing patterns first, then the missing wanted ones: nothing the admin set is lost."""
    patterns = list(existing)
    patterns.extend(p for p in wanted if p not in patterns)
    return patterns


def update_dto(current: Mapping[str, Any], *, exclude_patterns: Sequence[str]) -> dict[str, Any]:
    """A faithful `UpdateLibraryDto`: the library as read, with only the patterns replaced.

    The read model calls the file types `libraryFileTypes` and the write model `fileGroupTypes`.
    """
    missing = [name for name in _SAME if name not in current]
    if missing:
        raise ProtocolError(f"the library read lacks {missing}")
    dto: dict[str, Any] = {name: current[name] for name in _SAME}
    dto["folders"] = list(current.get("folders") or [])
    dto["fileGroupTypes"] = list(current.get("libraryFileTypes") or [])
    dto["excludePatterns"] = list(exclude_patterns)
    dto.update({name: current[name] for name in _OPTIONAL if name in current})
    return dto


def create_dto(
    name: str,
    library_type: LibraryType,
    folders: Sequence[str],
    exclude_patterns: Sequence[str] = HIDDEN_DIR_PATTERNS,
) -> dict[str, Any]:
    """A new library: Shelfhold owns its files, Kavita+ matching and scrobbling stay off."""
    file_types = (
        [_EPUB, _PDF] if library_type in (LibraryType.BOOK, LibraryType.LIGHT_NOVEL) else [_ARCHIVE]
    )
    return {
        "id": 0, "name": name, "type": int(library_type), "folders": list(folders),
        "folderWatching": True, "includeInDashboard": True, "includeInSearch": True,
        "manageCollections": True, "manageReadingLists": True, "allowScrobbling": False,
        "allowMetadataMatching": False, "enableMetadata": True, "removePrefixForSortName": False,
        "inheritWebLinksFromFirstChapter": False, "fileGroupTypes": file_types,
        "excludePatterns": list(exclude_patterns),
    }  # fmt: skip
