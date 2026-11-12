# SPDX-License-Identifier: MIT
"""ExportTarget contract v1 (02 section 4.2): destinations describe, the core calls.

UNSTABLE: provisional until M4. Only the shapes spelled out in the spec are modelled; ExportItem,
VersionRange, EventSubscription and the others are untyped dicts and will change, as will
``ApiCall.body``, which is missing. No third-party destination should be written against it yet.

The core holds every destination key and never hands one to a plugin. A destination returns pure
values or call descriptions (`ApiCall`, `FsOp`) that the host validates and executes. The richer
parts (version ranges, events, progress) are fixed by the lot that implements the first destination.
"""

from typing import Any, Literal, Protocol
from warnings import deprecated

from pydantic import Field

from shelfhold.sdk.fsops import ContentRef, FsOp
from shelfhold.sdk.models import SdkModel, StrictSdkModel

DEPRECATED_SINCE = "1.1"
"""Members that only describe how Kavita works are deprecated from SDK 1.1: they stay in the
contract (a destination written for SDK 1.0 still resolves, N-1 compatibility) and the contract
v2 that M4 decides, with the second adapter, takes them out (PLAN-M1b D9, objection 14)."""

KAVITA_ONLY = (
    f"Kavita-specific: deprecated since SDK {DEPRECATED_SINCE}, removed by contract v2 (M4)"
)

JsonObject = dict[str, Any]
"""Placeholder shape for the types the destination lot (M4) will refine: ExportItem, SidecarFile,
LibrarySpec, VersionRange, EventSubscription, RemoteRef, Progress."""


class NamingConstraints(SdkModel):
    max_segment_bytes: int
    max_path_bytes: int
    forbidden: str
    case_insensitive: bool


class TargetCapabilities(SdkModel):
    media_kinds: set[str]
    formats: set[str]  # cbz, epub, pdf, kepub (M6)
    transport: Literal["tree", "dropbox", "push", "pull"]  # "push" stays reserved until M4
    egress: Literal["foyer", "externe"]  # default value; the instance attests its own
    can_update: bool
    can_delete: bool
    can_purge: bool = False
    readback: Literal["none", "verify", "fetch"] = "none"
    confirms_arrival: bool
    reconcilable: bool
    accepted_content_types: list[str]
    sidecars: set[
        Literal["comicinfo", "opf-embedded", "series.json", "cover-file", "metadata.json"]
    ]
    path_scoped_scan: bool
    access_partitioning: Literal["none", "library", "user"]
    accepted_image_types: set[str]
    events: Literal["signalr", "sse", "none"]
    collections: Literal["api", "metadata", "none"]
    progress: Literal["api", "kosync", "none"]
    manage_libraries: bool
    naming: NamingConstraints
    name_normalizer: str
    series_grouping: Literal["name", "folder"] = "name"


class ApiCall(StrictSdkModel):
    """Described, not executed: the core runs it with its own secrets through its Fetcher."""

    method: str
    path: str  # relative: the host is fixed by the configuration
    query: dict[str, Any] = Field(default_factory=dict)
    json_body: Any = None
    auth: Literal["target_admin", "target_user", "none"]
    expect: list[int] = Field(default_factory=lambda: [200])


class ExportTarget(Protocol):
    capabilities: TargetCapabilities

    def config_schema(self) -> JsonObject: ...

    def default_template(self, kind: str) -> str: ...

    def render_sidecars(self, item: JsonObject, cfg: JsonObject) -> list[JsonObject]: ...

    @deprecated(KAVITA_ONLY)  # Kavita's `ParseSeriesName` check
    def parse_name_call(self, name: str, lib_type: str) -> ApiCall | None: ...

    def inventory_calls(self, remote_library_id: str) -> list[ApiCall]: ...

    @deprecated(KAVITA_ONLY)  # the creation of a Kavita library
    def ensure_library_call(self, spec: JsonObject, current: JsonObject | None) -> ApiCall: ...

    def scan_calls(self, remote_paths: list[str]) -> list[ApiCall]: ...

    @deprecated(KAVITA_ONLY)  # the lookup of a Kavita series by folder
    def resolve_remote_calls(self, item: JsonObject) -> list[ApiCall]: ...

    def interpret(self, call: ApiCall, response: JsonObject) -> Any: ...

    def supported_versions(self) -> JsonObject: ...

    @deprecated(KAVITA_ONLY)  # Kavita's SignalR hub
    def event_subscription(self) -> JsonObject | None: ...

    @deprecated(KAVITA_ONLY)  # Kavita's reading-progress API
    def progress_calls(self, ref: JsonObject, p: JsonObject | None) -> list[ApiCall]: ...


__all__ = [
    "DEPRECATED_SINCE",
    "ApiCall",
    "ContentRef",
    "ExportTarget",
    "FsOp",
    "NamingConstraints",
    "TargetCapabilities",
]
