"""The Komga destination as a describer of calls (ExportTarget, EF-EXP-020, EF-EXP-032).

Komga reads the **folder** as the series, where Kavita reads the **name**; ADR-0052 makes the two
coincide with a common render (`<root>/<Series>/<Series> - …`, series as leaves). Komga is an
indexer of that tree: it describes what lets it see the tree (a scan of its library, the
emptying of its trash after a deletion) and writes nothing in it. Its calls carry no secret at
all (`auth="target_admin"`): the core runs them through `KomgaClient`, which holds the key.

Komga has no scan of a folder, so `scan_calls` describes none: the only scan is the library's,
and the client asks for it once the writing has been quiet.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from shelfhold.export.targets.komga import routes
from shelfhold.sdk.export_target import ApiCall, NamingConstraints, TargetCapabilities

NO_FOLDER: Final = "the destination describes no scan of a folder: Komga rescans a library"
_EXTENSION: Final = re.compile(r"\.(cbz|cbr|epub|pdf|zip)$", re.IGNORECASE)
_SPACES: Final = re.compile(r"\s{2,}")


@dataclass(frozen=True)
class LayoutConflict:
    """A path where Komga and a reader of names would not see the same series."""

    rel_path: str
    reason: str  # nested_series_folder, file_at_library_root, name_differs_from_folder


def name_in_file(filename: str) -> str:
    """The series a reader of names finds at the head of a file name (`<Series> - Ch.001.cbz`)."""
    stem = _EXTENSION.sub("", filename.strip())
    head = stem.split(" - ", 1)[0].replace("_", " ")
    return _SPACES.sub(" ", head).strip(" \0\t\r-,")


def _same(a: str, b: str) -> bool:
    return unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b)


def _library_call(route: routes.Route, library_id: str) -> ApiCall:
    if not routes.LIBRARY_ID.fullmatch(library_id):
        raise ValueError("a library id is an identifier, never a path")
    return ApiCall(
        method=route.method,
        path=route.path.replace("{library_id}", library_id),
        auth="target_admin",
        expect=[202],  # Komga accepts the task and runs it later
    )


class KomgaDestination:
    capabilities = TargetCapabilities(
        media_kinds={"manga", "comic", "book"},
        formats={"cbz", "epub"},
        transport="tree",
        egress="foyer",
        can_update=False,
        can_delete=True,  # the bin of the host; `empty-trash` only forgets Komga's own records
        can_purge=False,
        confirms_arrival=False,
        reconcilable=False,
        accepted_content_types=["cbz", "epub"],
        sidecars={"comicinfo"},
        path_scoped_scan=False,  # Komga scans a library, never a folder
        # Nothing is partitioned until the audit of L08b exists: Komga is never given a shelf
        # below 18+ before it checks the libraries each account reaches (EF-EXP-047, T10).
        access_partitioning="none",
        accepted_image_types={"jpeg", "png", "webp"},
        events="none",
        collections="none",
        progress="none",
        manage_libraries=False,
        naming=NamingConstraints(
            max_segment_bytes=255, max_path_bytes=4096, forbidden='<>:"|?*', case_insensitive=False
        ),
        name_normalizer="komga-1",
        series_grouping="folder",  # a series is a folder (ADR-0052)
        indexes_tree=True,  # it indexes the tree Kavita (or the host) wrote; it never writes
    )

    # -- the calls ------------------------------------------------------------------------------

    def inventory_calls(self, remote_library_id: str) -> list[ApiCall]:
        return [ApiCall(method="GET", path=routes.LIBRARIES.path, auth="target_admin")]

    def scan_calls(self, remote_paths: Sequence[str]) -> list[ApiCall]:
        return []  # see NO_FOLDER: `scan_library_call` is the only scan there is

    def scan_library_call(self, library_id: str) -> ApiCall:
        """`POST /api/v1/libraries/{id}/scan`: one scan of the library, whatever the folders."""
        return _library_call(routes.LIBRARY_SCAN, library_id)

    def forget_removed_call(self, library_id: str) -> ApiCall:
        """`POST /api/v1/libraries/{id}/empty-trash`: Komga forgets the books it no longer finds."""
        return _library_call(routes.LIBRARY_EMPTY_TRASH, library_id)

    def supported_versions(self) -> dict[str, Any]:
        return {}  # the floor of Komga is a `known_bad_versions` row (L08b)

    def interpret(self, call: ApiCall, response: Mapping[str, Any]) -> Any:
        return None

    # -- the series Komga makes of a tree ---------------------------------------------------------

    def series_of(self, rel_paths: Sequence[str]) -> dict[str, list[str]]:
        """The series Komga makes of these files: one per folder that holds one, a file at the
        root being a one-shot series named after itself."""
        series: dict[str, list[str]] = defaultdict(list)
        for path in rel_paths:
            folder, _, filename = path.rpartition("/")
            series[folder or _EXTENSION.sub("", filename)].append(path)
        return dict(series)

    def layout_conflicts(self, rel_paths: Sequence[str]) -> list[LayoutConflict]:
        """Where the common render does not hold for Komga: a book folder under a series folder
        (Komga makes a series of each, Kavita merges them), a file at the root, a file name whose
        head is not its folder. The render is refused, never rewritten (files are frozen)."""
        found = []
        for path in rel_paths:
            parts = path.split("/")
            if len(parts) == 1:
                found.append(LayoutConflict(path, "file_at_library_root"))
            elif len(parts) > 2:
                found.append(LayoutConflict(path, "nested_series_folder"))
            elif not _same(name_in_file(parts[1]), parts[0]):
                found.append(LayoutConflict(path, "name_differs_from_folder"))
        return found
