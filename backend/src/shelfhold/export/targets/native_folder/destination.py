"""The native folder destination as a describer of plans (ExportTarget, EF-EXP-010).

A folder that Mihon, or the local source of Suwayomi, reads: `<root>/<Series>/Ch.<n> <title>.cbz`,
the cover as `cover.jpg`, the details in the `ComicInfo.xml` inside each archive (Title, Number and
Translator, which Mihon reads as the chapter's name, number and scanlator). No `*.json` ever: in a
series folder Mihon takes it for its legacy details file and deletes it. No RAR5: the archives are
CBZ, and an EPUB is one chapter.

It **describes** and never touches the disk: `describe` returns an `FsOpPlan`, which the executor of
the host runs. The bytes and the cover are the host's (`ContentRef`), never the destination's.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from shelfhold.export.domain.folders import fold
from shelfhold.export.domain.native_folder import cover_item_of, cover_ref
from shelfhold.export.targets.native_folder.names import (
    COVER_NAME,
    chapter_filename,
    series_folder,
)
from shelfhold.sdk.api_call import ApiCall
from shelfhold.sdk.export_target import NamingConstraints, TargetCapabilities
from shelfhold.sdk.fsops import ContentRef, FsOp, FsOpPlan

__all__ = [
    "ChapterSpec",
    "NativeFolderDestination",
    "SeriesSpec",
    "cover_item_of",
    "cover_ref",
]


@dataclass(frozen=True)
class ChapterSpec:
    """One chapter as the host planned it: the item that holds its bytes, its number and title,
    and the format the host serves this binding (`cbz` or `epub`)."""

    item_id: str
    number: str | None
    title: str
    format: Literal["cbz", "epub"]
    volume: bool = False  # a volume (several chapters in one file): `Vol.<n>`, not `Ch.<n>`


@dataclass(frozen=True)
class SeriesSpec:
    name: str
    chapters: tuple[ChapterSpec, ...]
    cover_item: str | None = None  # the item whose first page is the cover, when there is one


def _order(chapter: ChapterSpec) -> tuple[int, Decimal, str, str]:
    try:
        value = Decimal(chapter.number) if chapter.number else None
    except InvalidOperation:
        value = None
    return (0 if value is not None else 1, value or Decimal(0), chapter.title, chapter.item_id)


class NativeFolderDestination:
    capabilities = TargetCapabilities(
        media_kinds={"manga", "comic", "book"},
        formats={"cbz", "epub"},  # no RAR5, no PDF
        transport="tree",
        egress="foyer",
        can_update=False,
        can_delete=True,  # the bin of the host; nothing is ever purged here
        confirms_arrival=True,  # the file is on the disk once written: nothing else to wait for
        reconcilable=True,
        accepted_content_types=["cbz", "epub"],
        sidecars={"comicinfo", "cover-file"},
        path_scoped_scan=False,
        access_partitioning="none",  # a folder keeps no account apart: no shelf below 18+
        accepted_image_types={"jpeg", "png", "webp"},
        events="none",
        collections="none",
        progress="none",
        manage_libraries=False,
        naming=NamingConstraints(
            max_segment_bytes=255,
            max_path_bytes=4096,
            forbidden='<>:"|?*\\/',
            case_insensitive=False,
        ),
        name_normalizer="mihon-local-1",
        series_grouping="folder",
        # a `*.json` in a series folder is the legacy details file of Mihon: it consumes it
        forbidden_sidecars=frozenset({"series.json", "details.json", "metadata.json"}),
    )

    def config_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def default_template(self, kind: str) -> str:
        return "{series}/Ch.{chapter:0000.#} {title}"

    def render_sidecars(self, item: Mapping[str, Any], cfg: Mapping[str, Any]) -> list[Any]:
        return []  # the ComicInfo is made by the host inside each archive; the cover is a `sidecar`

    def inventory_calls(self, remote_library_id: str) -> list[ApiCall]:
        return []  # the host reads its own folder: no server to ask

    def scan_calls(self, remote_paths: Sequence[str]) -> list[ApiCall]:
        return []  # Mihon and Suwayomi look at the folder themselves

    def supported_versions(self) -> dict[str, Any]:
        return {}

    def interpret(self, call: ApiCall, response: Mapping[str, Any]) -> Any:
        return None

    def describe(self, series: Sequence[SeriesSpec]) -> FsOpPlan:
        """The plan for these series: a folder each, a file per chapter, the cover. Two chapters
        that would take one name keep both (` (2)`), two series that would take one folder share
        it."""
        folders: dict[str, str] = {}
        taken: dict[str, set[str]] = {}
        covers: dict[str, str | None] = {}
        chapters: dict[str, list[ChapterSpec]] = {}
        for spec in series:
            name = series_folder(spec.name)
            key = fold(name)
            folders.setdefault(key, name)
            chapters.setdefault(key, []).extend(spec.chapters)
            covers.setdefault(key, spec.cover_item)
            if covers[key] is None:
                covers[key] = spec.cover_item
        ops: list[FsOp] = []
        for key, folder in folders.items():
            ops.append(FsOp(op="mkdir", rel_path=folder))
            names = taken.setdefault(key, {fold(COVER_NAME)})
            for chapter in sorted(chapters[key], key=_order):
                filename = _unique(
                    chapter_filename(
                        chapter.number, chapter.title, chapter.format, volume=chapter.volume
                    ),
                    names,
                )
                ops.append(
                    FsOp(
                        op="write",
                        rel_path=f"{folder}/{filename}",
                        content_ref=ContentRef(kind="export_item", ref=chapter.item_id),
                    )
                )
            if covers[key] is not None:
                ops.append(
                    FsOp(
                        op="write_inline",
                        rel_path=f"{folder}/{COVER_NAME}",
                        content_ref=ContentRef(kind="sidecar", ref=cover_ref(str(covers[key]))),
                    )
                )
        return FsOpPlan(ops=tuple(ops))


def _unique(filename: str, taken: set[str]) -> str:
    stem, dot, extension = filename.rpartition(".")
    candidate, count = filename, 1
    while fold(candidate) in taken:
        count += 1
        candidate = f"{stem} ({count}){dot}{extension}"
    taken.add(fold(candidate))
    return candidate
