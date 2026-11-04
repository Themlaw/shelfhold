# SPDX-License-Identifier: MIT
"""ExportTarget contract v1 (02 section 4.2): destinations describe, the core calls.

UNSTABLE: provisional until M4. Only the shapes spelled out in the spec are modelled; ExportItem,
VersionRange, EventSubscription and the others are untyped dicts and will change, as will
``ApiCall.body``, which is missing. No third-party destination should be written against it yet.

The core holds every destination key and never hands one to a plugin. A destination returns pure
values or call descriptions (`ApiCall`, `FsOp`) that the host validates and executes. The richer
parts (version ranges, events, progress) are fixed by the lot that implements the first destination.
"""

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import Field

from shelfhold.sdk.models import SdkModel, StrictSdkModel

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


class ContentRef(StrictSdkModel):
    """Reference to bytes the host owns; a plan never carries raw content."""

    kind: Literal["export_item", "sidecar"]
    ref: str


class FsOp(StrictSdkModel):
    """Serialisable file operation plan; the host validates and executes it (passe 8, H3)."""

    op: Literal["mkdir", "write", "write_inline", "rename", "recycle", "touch_mtime", "unlink"]
    rel_path: str  # relative to the binding root; the host refuses any escape
    dst_rel_path: str | None = None
    content_ref: ContentRef | None = None
    mtime: datetime | None = None


class ExportTarget(Protocol):
    capabilities: TargetCapabilities

    def config_schema(self) -> JsonObject: ...

    def default_template(self, kind: str) -> str: ...

    def render_sidecars(self, item: JsonObject, cfg: JsonObject) -> list[JsonObject]: ...

    def parse_name_call(self, name: str, lib_type: str) -> ApiCall | None: ...

    def inventory_calls(self, remote_library_id: str) -> list[ApiCall]: ...

    def ensure_library_call(self, spec: JsonObject, current: JsonObject | None) -> ApiCall: ...

    def scan_calls(self, remote_paths: list[str]) -> list[ApiCall]: ...

    def resolve_remote_calls(self, item: JsonObject) -> list[ApiCall]: ...

    def interpret(self, call: ApiCall, response: JsonObject) -> Any: ...

    def supported_versions(self) -> JsonObject: ...

    def event_subscription(self) -> JsonObject | None: ...

    def progress_calls(self, ref: JsonObject, p: JsonObject | None) -> list[ApiCall]: ...
