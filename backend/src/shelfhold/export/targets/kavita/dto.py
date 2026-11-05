"""Shapes exchanged with Kavita, after the `openapi.json` frozen in the test fixtures."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Final

from shelfhold.export.targets.kavita.errors import ProtocolError

# Hidden folder of every physical root (EF-EXP-059): Kavita must never index it.
HIDDEN_DIR_PATTERNS: Final = ("**/.shelfhold/**",)
# Kavita's `MetadataProvider` enum starts at 2 and the server checks the provider against the
# library type (measured on 0.9.1.4): Mangabaka for manga and image, Hardcover for the others.
_PROVIDER_MANGABAKA: Final = 3
_PROVIDER_HARDCOVER: Final = 2


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


def _provider_for(library_type: LibraryType) -> int:
    manga_like = library_type in (LibraryType.MANGA, LibraryType.IMAGE)
    return _PROVIDER_MANGABAKA if manga_like else _PROVIDER_HARDCOVER


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
        # not required by the schema, but a real Kavita answers 400 on the default 0 and 500 on a
        # provider the type does not take (found by the nightly); unused while matching is off
        "metadataProvider": _provider_for(library_type),
    }  # fmt: skip


@dataclass(frozen=True)
class Member:
    """An account of Kavita as `GET /api/Users` describes it (read-only audit, EF-EXP-055)."""

    username: str
    is_admin: bool
    age_rating: int  # AgeRating of the restriction; NOT_APPLICABLE means none
    include_unknowns: bool
    library_ids: frozenset[int]
    created: str | None = None

    @classmethod
    def from_json(cls, data: object) -> Member:
        if not isinstance(data, dict):
            raise ProtocolError("users did not answer objects")
        restriction = data.get("ageRestriction")
        if not isinstance(restriction, dict) or "ageRating" not in restriction:
            # unreadable: counted as the most restricted account, so that the audit fails closed
            restriction = {"ageRating": AGE_UNKNOWN, "includeUnknowns": False}
        roles, libraries = data.get("roles") or [], data.get("libraries") or []
        if not isinstance(roles, list) or not all(isinstance(lib, dict) for lib in libraries):
            raise ProtocolError("users did not answer well-formed accounts")
        roles = [str(r) for r in roles]
        return cls(
            username=str(data.get("username") or ""),
            is_admin="Admin" in roles,
            age_rating=int(restriction.get("ageRating", AGE_NOT_APPLICABLE)),
            include_unknowns=bool(restriction.get("includeUnknowns", True)),
            library_ids=frozenset(int(lib["id"]) for lib in libraries if "id" in lib),
            created=str(data["createdUtc"]) if data.get("createdUtc") else None,
        )


AGE_NOT_APPLICABLE: Final = -1  # `AgeRating.NotApplicable`: no restriction
AGE_UNKNOWN: Final = 0  # `AgeRating.Unknown`


@dataclass(frozen=True)
class SeriesInfo:
    """A series as Kavita lists it: its current name and where it lives."""

    id: int
    name: str
    library_id: int
    folder_path: str
    lowest_folder_path: str
    name_locked: bool

    @classmethod
    def from_json(cls, data: object) -> SeriesInfo:
        if not isinstance(data, dict) or "id" not in data:
            raise ProtocolError("series did not answer an object")
        return cls(
            id=int(data["id"]),
            name=str(data.get("name") or ""),
            library_id=int(data.get("libraryId") or 0),
            folder_path=str(data.get("folderPath") or ""),
            lowest_folder_path=str(data.get("lowestFolderPath") or ""),
            name_locked=bool(data.get("nameLocked", False)),
        )

    def lives_in(self, folder: str) -> bool:
        """Kavita says `folderPath` (the library folder) and `lowestFolderPath` (the series')."""
        wanted = folder.rstrip("/")
        return wanted in (self.lowest_folder_path.rstrip("/"), self.folder_path.rstrip("/"))


@dataclass(frozen=True)
class SeriesAge:
    """The age of a series and whether anybody locked it (Kavita derives it from the tags)."""

    series_id: int
    age_rating: int
    locked: bool

    @classmethod
    def from_json(cls, data: object) -> SeriesAge:
        if not isinstance(data, dict):
            raise ProtocolError("series metadata did not answer an object")
        return cls(
            series_id=int(data.get("seriesId") or 0),
            age_rating=int(data.get("ageRating", AGE_UNKNOWN)),
            locked=bool(data.get("ageRatingLocked", False)),
        )


SERIES_FILTER_LIBRARIES: Final = (
    19  # `SeriesFilterField.Libraries` (18 is Genres: found by the nightly)
)
FILTER_EQUAL: Final = 0  # `FilterComparison.Equal`
FILTER_AND: Final = 1  # `FilterCombination.And`


def library_filter(library_id: int) -> dict[str, Any]:
    """A `SeriesFilterV2Dto` that lists the series of one library."""
    return {
        "id": 0, "name": "", "combination": FILTER_AND, "limitTo": 0,
        "statements": [
            {"comparison": FILTER_EQUAL, "field": SERIES_FILTER_LIBRARIES, "value": str(library_id)}
        ],
    }  # fmt: skip
