# SPDX-License-Identifier: MIT
"""ExportTarget contract v1 (02 section 4.2): destinations describe, the core calls.

SDK 1.4 (M4 L03) adds, all optional with a safe default so a 1.3 destination loads unchanged:
``ApiCall.body`` (`shelfhold.sdk.api_call`), the optional ``update_metadata`` capability, and the
`content_body` declaration that opens the ``push`` transport (behind the kit). ``readback`` and
``can_purge`` keep their 1.3 default.

UNSTABLE: ExportItem, VersionRange, EventSubscription and the others are still untyped dicts and
will change with the second adapter; write no third-party destination against them yet.

The core holds every destination key and never hands one to a plugin. A destination returns pure
values or call descriptions (`ApiCall`, `FsOp`) that the host validates and executes. The richer
parts (version ranges, events, progress) are fixed by the lot that implements the first destination.
"""

from collections.abc import Iterable
from fnmatch import fnmatchcase
from typing import Any, Literal, Protocol
from warnings import deprecated

from pydantic import field_serializer

from shelfhold.sdk.api_call import ApiCall
from shelfhold.sdk.fsops import ContentRef, FsOp
from shelfhold.sdk.models import SdkModel

DEPRECATED_SINCE = "1.1"
"""Members that only describe how Kavita works are deprecated from SDK 1.1: they stay in the
contract (a destination written for SDK 1.0 still resolves, N-1 compatibility) and the SDK
2.0, published in M6 with the kit, takes them out (PLAN-M1b D9, PLAN-M4 D7): none goes in 1.x."""

KAVITA_ONLY = f"Kavita-specific: deprecated since SDK {DEPRECATED_SINCE}, removed by SDK 2.0 (M6)"

JsonObject = dict[str, Any]
"""Placeholder shape for the types the destination lot (M4) will refine: ExportItem, SidecarFile,
LibrarySpec, VersionRange, EventSubscription, RemoteRef, Progress."""


class NamingConstraints(SdkModel):
    max_segment_bytes: int
    max_path_bytes: int
    forbidden: str
    case_insensitive: bool


class MetadataUpdateSupport(SdkModel):
    """What `update_metadata` accepts (SDK 1.4): the fields it can change in place, and whether it
    can lock them against a rescan. A destination without it is not asked."""

    fields: set[str]
    lock: bool = False


class MetadataChangeRoute(SdkModel):
    """How the host applies a metadata change to one destination."""

    update: set[str]
    rewrite_proposal: set[str]
    warn_rewrite: bool  # the warning of EF-EXP-037 goes with any rewrite proposal


class TargetCapabilities(SdkModel):
    media_kinds: set[str]
    formats: set[str]  # cbz, epub, pdf, kepub (M6)
    transport: Literal["tree", "dropbox", "push", "pull"]  # "push" needs `content_body` and the kit
    egress: Literal["foyer", "externe"]  # default value; the instance attests its own
    can_update: bool
    can_delete: bool
    can_purge: bool = False  # permanent deletion, distinct from the bin of `can_delete`
    readback: Literal["none", "verify", "fetch"] = "none"  # none: a copy is never read back
    content_body: bool = False  # SDK 1.4: sends `ApiCall.body` content; required by `push`
    update_metadata: MetadataUpdateSupport | None = None  # SDK 1.4: None = not supported
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
    indexes_tree: bool = False  # SDK 1.4 (ADR-0052): may index a tree it did not create
    forbidden_sidecars: frozenset[str] = frozenset()  # SDK 1.4: sidecars that harm it

    @field_serializer("forbidden_sidecars")
    def _sorted_sidecars(self, value: frozenset[str]) -> list[str]:
        return sorted(value)  # a stable JSON round trip


def sidecar_forbidden(forbidden: Iterable[str], filename: str) -> bool:
    """Is this file name one the destination forbids? Each entry is a file name or, when it holds a
    `*` or a `?`, a pattern (`*.json`); the comparison ignores the case."""
    name = filename.casefold()
    for entry in forbidden:
        rule = entry.casefold()
        if rule == name or (("*" in rule or "?" in rule) and fnmatchcase(name, rule)):
            return True
    return False


def copy_can_verify(caps: TargetCapabilities) -> bool:
    """A copy in the destination can be read back to check it (`verify` or `fetch`)."""
    return caps.readback != "none"


def copy_can_serve_bytes(caps: TargetCapabilities) -> bool:
    """A copy in the destination may serve as a source of bytes: only with `readback = fetch`."""
    return caps.readback == "fetch"


def metadata_change_route(caps: TargetCapabilities, changed: set[str]) -> MetadataChangeRoute:
    """Fields the destination updates in place, and the rest, which becomes a rewrite proposal
    (with the warning of EF-EXP-037). No `update_metadata` means every field is a proposal."""
    accepted = caps.update_metadata.fields if caps.update_metadata is not None else set()
    proposal = changed - accepted
    return MetadataChangeRoute(
        update=changed & accepted, rewrite_proposal=proposal, warn_rewrite=bool(proposal)
    )


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


class MetadataUpdater(Protocol):
    """The optional capability (SDK 1.4), apart from `ExportTarget`: a destination that declares
    `capabilities.update_metadata` implements it, and describes the calls that change `fields`
    in place, with `lock` to keep them against a rescan."""

    def update_metadata(self, ref: JsonObject, fields: JsonObject, lock: bool) -> list[ApiCall]: ...


__all__ = [
    "DEPRECATED_SINCE",
    "ApiCall",
    "ContentRef",
    "ExportTarget",
    "FsOp",
    "MetadataChangeRoute",
    "MetadataUpdateSupport",
    "MetadataUpdater",
    "NamingConstraints",
    "TargetCapabilities",
    "copy_can_serve_bytes",
    "copy_can_verify",
    "metadata_change_route",
    "sidecar_forbidden",
]
