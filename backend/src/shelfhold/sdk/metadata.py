# SPDX-License-Identifier: MIT
"""MetadataProvider contract v1.2, typed by entity (EF-BIB-013, ENF-LEG-003).

A provider answers a question about one *target* (`EntityRef`): a Series, a Work, an Edition or a
Volume. An ISBN names an Edition or a Volume, never a Work, so a Work-level fact learnt from an ISBN
travels as a `RelationProposal`, not as a field of the Work. Results are proposals with provenance:
the host decides what to keep, and never overwrites a locked field (EF-BIB-008).

Shape: designed from the concept of a metadata source (identify, cover, URL <-> identifier mapping,
declared touched fields, declared quota); no third-party code is reused. This module is MIT and
never imports the AGPL core (ENF-LEG-010). Like every SDK type it round-trips through JSON.
"""

from datetime import datetime
from enum import Flag, auto
from typing import Annotated, Any, Literal, Protocol, Self

from pydantic import (
    Field,
    JsonValue,
    PlainSerializer,
    PlainValidator,
    WithJsonSchema,
    model_validator,
)

from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.lang import LanguageTag
from shelfhold.sdk.models import (
    AuthFlow,
    PluginKind,
    PluginManifest,
    SdkModel,
    SessionState,
    flag_names,
    flag_parser,
)
from shelfhold.sdk.refs import ImageRequest

EntityType = Literal["series", "work", "edition", "volume"]

ISBN_SCHEMES = frozenset({"isbn10", "isbn13"})
ISBN_TARGETS: frozenset[str] = frozenset({"edition", "volume"})
"""Entity types an ISBN may designate."""


class ProviderCapability(Flag):
    IDENTIFY = auto()
    COVER = auto()
    RELEASE_CALENDAR = auto()


ProviderCapabilitySet = Annotated[
    ProviderCapability,
    PlainValidator(flag_parser(ProviderCapability)),
    PlainSerializer(flag_names, return_type=list[str]),
    WithJsonSchema({"type": "array", "items": {"type": "string"}}),
]


class ProviderQuota(SdkModel):
    """Token bucket plus an optional daily ceiling, declared in the manifest (ENF-LEG-005)."""

    requests_per_second: float
    burst: int = 1
    daily_cap: int | None = None


class MetadataManifest(PluginManifest):
    kind: PluginKind = PluginKind.METADATA
    min_sdk: str = "1.2"  # first SDK minor that has this contract
    capabilities: ProviderCapabilitySet = ProviderCapability.IDENTIFY
    touched_fields: set[str]  # fields this provider may fill: nothing else is proposed
    entity_types: set[EntityType]  # entities it can be asked about
    langs: list[LanguageTag] = Field(default_factory=list)
    quota: ProviderQuota
    auth: AuthFlow | None = None  # reuses EF-SRC-023 (per-user OAuth for some providers)


class EntityRef(SdkModel):
    """What the host asks about: a type and the identifiers it knows (scheme -> value)."""

    entity_type: EntityType
    ids: dict[str, str] = Field(default_factory=dict)  # "isbn13", "openlibrary", ...
    entity_id: str | None = None  # opaque host identifier, echoed back, never interpreted

    @model_validator(mode="after")
    def _isbn_targets_edition_or_volume(self) -> Self:
        isbns = ISBN_SCHEMES & self.ids.keys()
        if isbns and self.entity_type not in ISBN_TARGETS:
            raise ValueError(
                f"an ISBN designates an edition or a volume, not a {self.entity_type}: {isbns}"
            )
        if not self.ids and self.entity_id is None:
            raise ValueError("an EntityRef needs at least one identifier")
        return self


class MatchHints(SdkModel):
    """Soft information that helps a provider choose between candidates."""

    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    lang: LanguageTag | None = None
    year: int | None = None


class Provenance(SdkModel):
    """Where a proposal comes from: kept with the value (EF-BIB-008, ENF-LEG-005)."""

    provider: str  # plugin key
    fetched_at: datetime
    source_url: str | None = None  # free of secrets
    licence: str | None = None
    attribution: str | None = None


class FieldProposal(SdkModel):
    entity_type: EntityType
    field: str
    lang: LanguageTag | None = None
    value: JsonValue
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    provenance: Provenance


RelationKind = Literal[
    "work_relation", "official_link", "licensed_in", "release", "age_suggestion", "contributor"
]


class RelationProposal(SdkModel):
    """A link between entities, or a fact that is not a plain field (calendar, licence, age)."""

    kind: RelationKind
    subject: EntityRef
    object: EntityRef | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    provenance: Provenance


class IdentifyResult(SdkModel):
    proposals: list[FieldProposal] = Field(default_factory=list)
    relations: list[RelationProposal] = Field(default_factory=list)


class MetadataProvider(Protocol):
    """A metadata provider. `get_cover` exists when COVER is declared, `login` and `refresh` when
    `manifest.auth` is."""

    manifest: MetadataManifest

    async def identify(
        self, f: Fetcher, target: EntityRef, hints: MatchHints
    ) -> IdentifyResult: ...

    async def get_cover(self, f: Fetcher, target: EntityRef) -> ImageRequest | None: ...

    def url_for(self, ids: dict[str, str]) -> str | None: ...

    def ids_from_url(self, url: str) -> dict[str, str] | None: ...

    async def login(self, f: Fetcher, flow: AuthFlow, creds: dict[str, Any]) -> SessionState: ...

    async def refresh(self, f: Fetcher, s: SessionState) -> SessionState: ...


__all__ = [
    "ISBN_SCHEMES",
    "ISBN_TARGETS",
    "EntityRef",
    "EntityType",
    "FieldProposal",
    "IdentifyResult",
    "MatchHints",
    "MetadataManifest",
    "MetadataProvider",
    "Provenance",
    "ProviderCapability",
    "ProviderQuota",
    "RelationKind",
    "RelationProposal",
]
