# SPDX-License-Identifier: MIT
"""Two synthetic destinations, reserved for later (M4 L11, PLAN-M4 D9): no adapter ships.

They only prove that the SDK 1.4 contract can express them without a break. Each one describes
(`TargetCapabilities`, `ApiCall`); neither touches the disk or the network. Versions are plain
strings in `supported_versions()`, since the SDK has no generic version type.
"""

from typing import Any
from urllib.parse import quote

from shelfhold_sdk.api_call import ApiCall, ContentBody
from shelfhold_sdk.export_target import (
    JsonObject,
    MetadataUpdateSupport,
    NamingConstraints,
    TargetCapabilities,
)
from shelfhold_sdk.fsops import ContentRef

_NAMING = NamingConstraints(
    max_segment_bytes=255, max_path_bytes=4096, forbidden='<>:"|?*', case_insensitive=False
)


class CalibreReservedTarget:
    """EF-EXP-023: push over the `/cdb/*` routes of the content server, raw body, no field lock."""

    capabilities = TargetCapabilities(
        media_kinds={"book"},
        formats={"epub", "pdf"},
        transport="push",
        egress="foyer",
        can_update=True,
        can_delete=True,
        content_body=True,
        update_metadata=MetadataUpdateSupport(
            fields={"title", "authors", "series", "tags", "cover"}, lock=False
        ),  # calibre has no field lock: `lock` is ignored, and declared so
        confirms_arrival=True,
        reconcilable=True,
        accepted_content_types=["epub", "pdf"],
        sidecars=set(),
        path_scoped_scan=False,
        access_partitioning="library",
        accepted_image_types={"jpeg"},
        events="none",
        collections="none",
        progress="none",
        manage_libraries=False,
        naming=_NAMING,
        name_normalizer="calibre-1",
    )

    def add_book_call(self, content: ContentRef, *, filename: str, library_id: str) -> ApiCall:
        # `add_duplicates = n` is the idempotence; job id 0, the host does not follow the job
        path = f"/cdb/add-book/0/n/{quote(filename, safe='')}/{quote(library_id, safe='')}"
        return ApiCall(
            method="POST", path=path, body=ContentBody(content_ref=content), auth="target_admin"
        )

    def update_metadata(self, ref: JsonObject, fields: JsonObject, lock: bool) -> list[ApiCall]:
        """`/cdb/set-fields/{book_id}`; `lock` is ignored, calibre has no field lock."""
        book_id = quote(str(ref["remote_id"]), safe="")
        return [
            ApiCall(
                method="POST",
                path=f"/cdb/set-fields/{book_id}",
                json_body={"changes": fields},
                auth="target_admin",
            )
        ]

    def interpret(self, call: ApiCall, response: JsonObject) -> JsonObject:
        """`book_id` is the `remote_id` locator of the copy."""
        return {"remote_id": str(response["book_ids"][0])}

    def supported_versions(self) -> JsonObject:
        # the `/cdb/*` routes are internal to calibre: a narrow range, pinned by a nightly test
        return {"minimum": "7.0", "maximum_exclusive": "9.0", "known_bad_versions": []}


class AudiobookshelfReservedTarget:
    """EF-EXP-035: a file tree and a targeted scan, never the upload route."""

    capabilities = TargetCapabilities(
        media_kinds={"audio"},
        formats=set(),
        transport="tree",
        egress="foyer",
        can_update=True,
        can_delete=True,
        confirms_arrival=False,
        reconcilable=True,
        accepted_content_types=[],
        sidecars={"metadata.json", "cover-file"},
        path_scoped_scan=True,
        access_partitioning="library",
        accepted_image_types={"jpeg"},
        events="none",
        collections="none",
        progress="none",
        manage_libraries=False,
        naming=_NAMING,
        name_normalizer="audiobookshelf-1",
        series_grouping="folder",
    )

    def scan_calls(self, remote_paths: list[str]) -> list[ApiCall]:
        """One targeted scan per leaf folder; `POST /api/upload` is never called."""
        return [
            ApiCall(
                method="POST",
                path="/api/watcher/update",
                json_body={"path": path},
                auth="target_admin",
            )
            for path in remote_paths
        ]

    def supported_versions(self) -> JsonObject:
        known_bad: list[dict[str, Any]] = [
            # an authenticated user downloads files of libraries closed to them: breaks
            # `access_partitioning = library`
            {"advisory": "CVE-2026-42883", "category": "access_control", "below": "2.32.2"}
        ]
        # the floor of ENF-SEC-016 is kept here only: `docs/security/dependances.md` gets the
        # entry the day EF-EXP-035 is written
        return {"minimum": "2.37", "known_bad_versions": known_bad}
