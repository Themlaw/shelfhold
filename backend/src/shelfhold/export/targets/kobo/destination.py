"""The Kobo folder destination as a describer of plans (ExportTarget, EF-EXP-021, D19).

A folder the household's sync tool, or the mounted reader, reads: `<root>/<Series>/<book>
.kepub.epub`, one file per book, the extension a Kobo wants (mandatory — it is what makes the
reader give the book its spans). The bytes are not the destination's: each `write` cites the
`export_item` the host planned, and the host serves the KEPUB of the item's **neutralized
derivative** (I35), made by the Python conversion of M5 through the rendition cache — never the
received original, never a binary.

It **describes** and never touches the disk: `describe` returns an `FsOpPlan`, which the executor
of the host runs. A folder keeps no account apart (`access_partitioning = none`): a shelf below
18+ is refused by the rule every such destination meets, at the binding and at every pass.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from shelfhold_sdk.api_call import ApiCall
from shelfhold_sdk.export_target import NamingConstraints, TargetCapabilities
from shelfhold_sdk.fsops import ContentRef, FsOp, FsOpPlan

from shelfhold.export.domain.folders import fold
from shelfhold.export.domain.naming import MAX_COMPONENT_BYTES
from shelfhold.export.targets.kobo.names import book_filename, series_folder

__all__ = ["BookSpec", "KoboDestination", "SeriesSpec"]


@dataclass(frozen=True)
class BookSpec:
    """One book as the host planned it: the item that holds its bytes, its number and title."""

    item_id: str
    number: str | None
    title: str
    volume: bool = False  # a volume (several units in one file): `Vol.<n>`, not `Ch.<n>`


@dataclass(frozen=True)
class SeriesSpec:
    name: str
    books: tuple[BookSpec, ...]


class KoboDestination:
    capabilities = TargetCapabilities(
        media_kinds={"book"},  # a Kobo reads books; a folder of images is another destination
        formats={"kepub"},  # the KEPUB of M5 (EF-BIB-015): the extension is the destination's
        transport="tree",  # a folder the reader mounts, or that a sync tool of the home reads
        egress="foyer",
        can_update=False,
        can_delete=True,  # the reader's own folder: the bin of the host, nothing purged here
        confirms_arrival=True,  # the file is on the disk once written: nothing else to wait for
        reconcilable=True,
        accepted_content_types=["kepub", "epub"],  # a KEPUB is made from the EPUB (GAP-6-10)
        sidecars=set(),
        path_scoped_scan=False,
        access_partitioning="none",  # a folder keeps no account apart: no shelf below 18+
        accepted_image_types={"jpeg", "png", "webp"},
        events="none",
        collections="none",
        progress="none",  # the reading state of the device is the sync surface's, not a file's
        manage_libraries=False,
        naming=NamingConstraints(
            max_segment_bytes=MAX_COMPONENT_BYTES,
            max_path_bytes=4096,
            forbidden='<>:"|?*\\/',
            case_insensitive=False,
        ),
        name_normalizer="kobo-1",
        series_grouping="folder",
    )

    def config_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def default_template(self, kind: str) -> str:
        return "{series}/Ch.{chapter:0000.#} {title}"

    def render_sidecars(self, item: Any, cfg: Any) -> list[Any]:
        return []  # a Kobo folder takes the book and nothing else

    def inventory_calls(self, remote_library_id: str) -> list[ApiCall]:
        return []  # the host reads its own folder: no server to ask

    def scan_calls(self, remote_paths: Sequence[str]) -> list[ApiCall]:
        return []  # the reader, or the sync tool of the home, looks at the folder itself

    def supported_versions(self) -> dict[str, Any]:
        return {}

    def interpret(self, call: ApiCall, response: Any) -> Any:
        return None

    def describe(self, series: Iterable[SeriesSpec]) -> FsOpPlan:
        """The plan for these series: a folder each, a `.kepub.epub` per book. Two books that
        would take one name keep both (` (2)`), two series that would take one folder share it."""
        folders: dict[str, str] = {}
        taken: dict[str, set[str]] = {}
        books: dict[str, list[BookSpec]] = {}
        for spec in series:
            name = series_folder(spec.name)
            key = fold(name)
            folders.setdefault(key, name)
            books.setdefault(key, []).extend(spec.books)
        ops: list[FsOp] = []
        for key, folder in folders.items():
            ops.append(FsOp(op="mkdir", rel_path=folder))
            names = taken.setdefault(key, set())
            for book in sorted(books[key], key=lambda b: b.item_id):
                filename = _unique(
                    book_filename(book.number, book.title, volume=book.volume), names
                )
                ops.append(
                    FsOp(
                        op="write",
                        rel_path=f"{folder}/{filename}",
                        content_ref=ContentRef(kind="export_item", ref=book.item_id),
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
