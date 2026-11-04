# SPDX-License-Identifier: MIT
"""The Source contract v1 (EF-SRC-001) and its optional capabilities."""

from enum import Flag, auto
from typing import Annotated, Any, Literal, Protocol

from pydantic import Field, PlainSerializer, PlainValidator, WithJsonSchema

from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.lang import LanguageTag
from shelfhold.sdk.models import (
    AuthFlow,
    Cursor,
    FilterSpec,
    Page,
    PluginManifest,
    RateLimit,
    SdkModel,
    SessionState,
    flag_names,
    flag_parser,
)
from shelfhold.sdk.refs import (
    ImageRequest,
    Nature,
    PageRef,
    RangedPayload,
    RangeRef,
    SeriesInfo,
    SeriesRef,
    UnitInfo,
    UnitPayload,
)


class Capability(Flag):
    SEARCH = auto()
    LATEST = auto()
    POPULAR = auto()
    URL_RESOLVE = auto()
    LOGIN = auto()
    FILTERS = auto()
    METADATA_ONLY = auto()
    FOLLOW = auto()
    SUB_SOURCES = auto()
    RANGED_CONTENT = auto()  # EF-SRC-032: one file for a range of Units (lncrawl, M2)


CapabilitySet = Annotated[
    Capability,
    PlainValidator(flag_parser(Capability)),
    PlainSerializer(flag_names, return_type=list[str]),
    WithJsonSchema({"type": "array", "items": {"type": "string"}}),
]


class SubSource(SdkModel):
    """Dynamic sub-source of a meta-source (EF-SRC-021)."""

    source_key: str  # e.g. "suwayomi:2499283573021220255"
    name: str
    lang: LanguageTag | Literal["mul"]  # "mul": multilingual, never copied into an Edition
    content_warning: Literal["safe", "mixed", "nsfw"]
    limit_key: str  # rate-limit key of the logical upstream source


class SourceTestCase(SdkModel):
    """Synthetic offline cassette of a source, with an optional authorised canary."""

    name: str
    cassette: str  # path relative to the plugin, synthetic content only
    canary: bool = False


RightsBasis = Literal["us_public_domain", "worldwide_public_domain", "licence", "user_supplied"]


class SourceManifest(PluginManifest):
    langs: list[LanguageTag]
    content_kinds: set[Literal["images", "text", "file"]]
    url_patterns: list[str]
    capabilities: CapabilitySet
    rate_limit: RateLimit
    auth: list[AuthFlow] = Field(default_factory=list)
    id_scheme: str = "v1"  # a change makes the host expect aliases (EF-SRC-022)
    rights_basis: RightsBasis | None = None
    rights_jurisdiction: str | None = None
    translation_nature: Nature = "unknown"
    # Trust in the BODY of the files (EF-BIB-051, ADR-0047). A silent plugin is untrusted:
    # an untrusted EPUB gets its neutralised derivative before any export.
    content_trust: Literal["publisher", "untrusted"] = "untrusted"
    tests: list[SourceTestCase] = Field(default_factory=list)


class SourceError(Exception):
    """Typed error of a source; `code` drives the health state and the UI action (EF-SRC-014)."""

    code: str = "source.error"


class AuthRequired(SourceError):
    code = "source.auth_required"


class RateLimited(SourceError):
    code = "source.rate_limited"

    def __init__(self, message: str = "", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ContentRemoved(SourceError):
    code = "source.content_removed"


class SourceChanged(SourceError):
    code = "source.changed"  # the parser broke


class TemporarilyUnavailable(SourceError):
    code = "source.temporarily_unavailable"  # upstream asleep or down: pause, not failure


class Source(Protocol):
    """A source. Methods tied to a capability exist only when the manifest declares it."""

    manifest: SourceManifest

    def filters(self) -> list[FilterSpec]: ...

    async def sub_sources(self, f: Fetcher) -> list[SubSource]: ...  # SUB_SOURCES

    async def search(
        self,
        f: Fetcher,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]: ...

    async def resolve_url(self, f: Fetcher, url: str) -> SeriesRef | UnitInfo | None: ...

    async def get_series(self, f: Fetcher, ref: SeriesRef) -> SeriesInfo: ...

    async def list_units(self, f: Fetcher, ref: SeriesRef) -> list[UnitInfo]: ...

    async def fetch_unit(self, f: Fetcher, ref: SeriesRef, u: UnitInfo) -> UnitPayload: ...

    # RANGED_CONTENT: the synchronous case of the RangeRef target of EF-SRC-034.
    async def fetch_range(self, f: Fetcher, target: RangeRef) -> RangedPayload: ...

    async def resolve_page(self, f: Fetcher, u: UnitInfo, p: PageRef) -> ImageRequest: ...

    async def login(self, f: Fetcher, flow: AuthFlow, creds: dict[str, Any]) -> SessionState: ...

    async def refresh(self, f: Fetcher, s: SessionState) -> SessionState: ...

    def migrate_ref(self, old_source_key: str, old_remote_id: str) -> tuple[str, str] | None: ...

    def migrate_memo(self, version: int, memo: dict[str, Any]) -> dict[str, Any]: ...
