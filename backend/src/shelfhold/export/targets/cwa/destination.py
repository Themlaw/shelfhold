"""The CWA drop box as a destination (EF-EXP-022, EF-EXP-047, EF-EXP-058, ADR-0052).

A drop box is a folder that an ingester watches: it takes a finished EPUB, imports it, and
**removes** the file. So the destination declares one transport, `dropbox`, an EPUB at a time, no
tree (nothing is indexed, no folder is managed), no sentinel and no detection of another writer
(`reconcilable` is false: the folder is meant to be emptied by someone else), no account kept
apart (`access_partitioning = none`: a shelf below 18+ is never bound to it). It **describes** a
plan of `write` operations and touches nothing: the executor of the host writes each file aside
and renames it into the folder, and the manifest follows the drop by an `idempotency_key`, since
there is no path to look at once the file is gone. Pure: no disk, no database, no secret.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from shelfhold_sdk.api_call import ApiCall
from shelfhold_sdk.export_target import NamingConstraints, TargetCapabilities
from shelfhold_sdk.fsops import ContentRef, FsOp, FsOpPlan

from shelfhold.export.domain.naming import MAX_COMPONENT_BYTES, sanitize_name

__all__ = ["CwaDestination", "DropSpec", "drop_name"]

EXTENSION: Final = ".epub"
_ITEM_ID: Final = re.compile(r"[0-9A-Za-z_-]{1,64}")  # an identifier: no separator, no dot


@dataclass(frozen=True)
class DropSpec:
    """One book to drop: the export item the host serves the bytes of, and a title for a human
    who looks at the folder."""

    item_id: str
    title: str


def drop_name(item_id: str, title: str) -> str:
    """`<title> [<item id>].epub`: one component, never hidden, never longer than a file name may
    be, and **unique per item** (two books with one title never meet). The item identifier is what
    ties a file in the folder to the manifest."""
    if not _ITEM_ID.fullmatch(item_id):
        raise ValueError("an item identifier is letters, digits, `_` and `-`")
    tail = f" [{item_id}]{EXTENSION}"
    room = MAX_COMPONENT_BYTES - len(tail.encode())
    return f"{sanitize_name(title, max_bytes=room)}{tail}"


class CwaDestination:
    capabilities = TargetCapabilities(
        media_kinds={"manga", "comic", "book"},
        formats={"epub"},
        transport="dropbox",
        egress="foyer",
        can_update=False,
        can_delete=False,
        confirms_arrival=False,
        reconcilable=False,
        accepted_content_types=["epub"],
        sidecars=set(),
        path_scoped_scan=False,
        access_partitioning="none",
        accepted_image_types={"jpeg", "png", "webp"},
        events="none",
        collections="none",
        progress="none",
        manage_libraries=False,
        naming=NamingConstraints(
            max_segment_bytes=MAX_COMPONENT_BYTES,
            max_path_bytes=MAX_COMPONENT_BYTES,  # the folder itself: no sub-folder is ever made
            forbidden='<>:"|?*\\/',
            case_insensitive=False,
        ),
        name_normalizer="cwa-1",
    )

    def config_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def default_template(self, kind: str) -> str:
        return "{title}"  # the name is made by `drop_name`: the folder is flat

    def render_sidecars(self, item: Any, cfg: Any) -> list[Any]:
        return []

    def inventory_calls(self, remote_library_id: str) -> list[ApiCall]:
        return []

    def scan_calls(self, remote_paths: list[str]) -> list[ApiCall]:
        return []  # the ingester watches its folder by itself: nobody is asked to scan

    def supported_versions(self) -> dict[str, Any]:
        return {}

    def interpret(self, call: ApiCall, response: Any) -> Any:
        return None

    def describe(self, books: Iterable[DropSpec]) -> FsOpPlan:
        """One `write` per book, at the root of the ingestion folder, citing the item whose bytes
        the host serves. No `mkdir`, no sub-folder, no sidecar, never a second file for a book."""
        ops = [
            FsOp(
                op="write",
                rel_path=drop_name(spec.item_id, spec.title),
                content_ref=ContentRef(kind="export_item", ref=spec.item_id),
            )
            for spec in books
        ]
        return FsOpPlan(ops=tuple(ops))
