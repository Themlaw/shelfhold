"""The Kavita destination as a describer of calls (ExportTarget, EF-EXP-001).

It returns `ApiCall`s with `auth="target_admin"` and no secret at all: the core runs them through
`KavitaClient.execute`, which holds the key. Only the two calls the client sends are here; the
rest of the contract (inventory, library, versions) comes with the lot that uses it (M1b).
"""

from __future__ import annotations

from collections.abc import Sequence

from shelfhold.export.targets.kavita import dto, routes
from shelfhold.sdk.export_target import ApiCall


class KavitaDestination:
    def parse_name_call(self, name: str, lib_type: str) -> ApiCall | None:
        """`plugin/parse` checks a file **name** with the library type, nothing else."""
        return ApiCall(
            method=routes.PARSE.method,
            path=routes.PARSE.path,
            query={"name": name, "libraryType": int(dto.LibraryType.from_name(lib_type))},
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
