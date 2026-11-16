"""The Kavita destination as a describer of calls (ExportTarget, EF-EXP-001).

It returns `ApiCall`s with `auth="target_admin"` and no secret at all: the core runs them through
`KavitaClient.execute`, which holds the key. Only the part of the contract L25 needs is here; the
rest (sidecars, inventory, progress) comes with the lots that use it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from shelfhold.export.domain.kavita_compat import MAXIMUM_EXCLUSIVE, MINIMUM_VERSION
from shelfhold.export.targets.kavita import dto, routes
from shelfhold.sdk.export_target import ApiCall, NamingConstraints, TargetCapabilities


class KavitaDestination:
    capabilities = TargetCapabilities(
        media_kinds={"manga", "comic", "book"},
        formats={"cbz", "epub"},
        transport="tree",
        egress="foyer",
        can_update=True,
        can_delete=True,  # the bin of the host; Kavita itself never purges (`can_purge` stays off)
        confirms_arrival=True,
        reconcilable=True,
        accepted_content_types=["cbz", "epub"],
        sidecars={"comicinfo"},
        path_scoped_scan=True,
        access_partitioning="library",
        accepted_image_types={"jpeg", "png", "webp", "gif"},  # pages and covers (EF-EXP-046)
        events="signalr",
        collections="api",
        progress="api",
        manage_libraries=True,
        naming=NamingConstraints(
            max_segment_bytes=255, max_path_bytes=4096, forbidden='<>:"|?*', case_insensitive=False
        ),
        name_normalizer="kavita-1",
        series_grouping="folder",
        forbidden_sidecars=frozenset({"series.json"}),  # Kavita misreads it (ADR-0052, M4 L03)
    )

    def parse_name_call(self, name: str, lib_type: str) -> ApiCall | None:
        """`plugin/parse` checks a file **name** with the library type, nothing else."""
        return ApiCall(
            method=routes.PARSE.method,
            path=routes.PARSE.path,
            query={"name": name, "libraryType": int(dto.LibraryType.from_name(lib_type))},
            auth="target_admin",
        )

    def inventory_calls(self, remote_library_id: str) -> list[ApiCall]:
        return [ApiCall(method="GET", path=routes.LIBRARIES.path, auth="target_admin")]

    def ensure_library_call(
        self, spec: Mapping[str, Any], current: Mapping[str, Any] | None
    ) -> ApiCall:
        """GET then faithful PUT: `current` is the library as read, or None to create it."""
        wanted = list(spec.get("exclude_patterns", dto.HIDDEN_DIR_PATTERNS))
        if current is None:
            body = dto.create_dto(
                str(spec["name"]),
                dto.LibraryType.from_name(str(spec["type"])),
                spec["folders"],
                wanted,
            )
            return ApiCall(
                method="POST", path=routes.LIBRARY_CREATE.path, json_body=body, auth="target_admin"
            )
        patterns = dto.merged_patterns(current.get("excludePatterns") or [], wanted)
        return ApiCall(
            method="POST",
            path=routes.LIBRARY_UPDATE.path,
            json_body=dto.update_dto(current, exclude_patterns=patterns),
            auth="target_admin",
        )

    def scan_calls(self, remote_paths: Sequence[str]) -> list[ApiCall]:
        """One `scan-folder` per folder. The core adds `apiKey` to the body at send time."""
        return [
            ApiCall(
                method=routes.SCAN_FOLDER.method,
                path=routes.SCAN_FOLDER.path,
                json_body={"folderPath": path, "abortOnNoSeriesMatch": False},
                auth="target_admin",
            )
            for path in remote_paths
        ]

    def supported_versions(self) -> dict[str, Any]:
        return {"minimum": str(MINIMUM_VERSION), "maximum_exclusive": str(MAXIMUM_EXCLUSIVE)}
