# SPDX-License-Identifier: MIT
"""References, units and payloads exchanged between a source and the host.

References are opaque, carry no secret and stay stable when credentials rotate or the connected
tool is reinstalled (EF-SRC-035, EF-SRC-036). They are never the database id of the tool.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from shelfhold.sdk.age import AgeRating
from shelfhold.sdk.lang import LanguageTag
from shelfhold.sdk.models import SdkModel

Nature = Literal["published", "community", "machine", "unknown"]


class ImageRequest(SdkModel):
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    referer: str | None = None
    method: Literal["GET", "POST"] = "GET"
    body: bytes | None = None
    expires_at: datetime | None = None  # a signed URL that expires is resolved again


class ScrambleInfo(SdkModel):
    processor: str  # key of a "descramble" Processor (reserved)
    params: dict[str, Any] = Field(default_factory=dict)


class PageRef(SdkModel):
    """Lazy page: resolved on demand by `Source.resolve_page`."""

    index: int
    token: str
    scrambled: ScrambleInfo | None = None


class SeriesRef(SdkModel):
    source_key: str
    remote_id: str  # relative url or id; opaque and free of secrets
    title: str
    cover: ImageRequest | None = None
    memo: dict[str, Any] = Field(default_factory=dict)
    memo_version: int = 1


class SeriesInfo(SdkModel):
    ref: SeriesRef
    title: str
    description: str | None = None
    authors: list[str] = Field(default_factory=list)  # writers, as the source spells them
    artists: list[str] = Field(default_factory=list)  # illustrators, as the source spells them
    external_ids: dict[str, str] = Field(default_factory=dict)
    series_hint: str | None = None
    age_rating: AgeRating | None = None  # suggested by the source; the host decides (EF-EXP-011)


class UnitInfo(SdkModel):
    remote_unit_id: str  # stable identity in the upstream source (ADR-0021, EF-SRC-036)
    source_order: int  # order as the source gives it
    number: str | None = None  # raw text ("12.5", "Extra 2", "Vol.3 Ch.10")
    number_sort: Decimal | None = None  # proposed sort key; the host may recompute it
    part: int | None = None
    track: Literal["main", "annual", "special", "extra", "oneshot"] = "main"
    kind: Literal["chapter", "episode", "book"] = "chapter"
    title: str | None = None
    volume: str | None = None
    volume_label: str | None = None  # "Vol.N" read in the chapter name -> unit_coverage
    volume_closed: bool | None = None  # the source announces the volume is closed (EF-SRC-032)
    team: str | None = None
    translation_nature: Nature | None = None
    lang: LanguageTag | None = None
    lang_raw: str | None = None
    published_at: datetime | None = None  # a zero upload date is None, never 1970-01-01
    published_on: str | None = None  # ISO 8601 partial ("2023", "2023-05", "2023-05-14")
    date_precision: Literal["year", "month", "day"] | None = None  # with published_on, never finer
    availability: Literal["free", "purchased", "expiring", "locked", "stubbed", "removed"] = "free"
    available_until: datetime | None = None
    revision_hint: str | None = None
    memo: dict[str, Any] = Field(default_factory=dict)
    memo_version: int = 1


class UnitRef(SdkModel):
    """Target of a request: one Unit of a series (EF-SRC-034)."""

    series: SeriesRef
    unit: UnitInfo


class RangeRef(SdkModel):
    """Target of a request: a range of Units of one Edition (EF-SRC-034)."""

    series: SeriesRef
    first: UnitInfo
    last: UnitInfo
    volume_label: str | None = None


class ImagePages(SdkModel):
    kind: Literal["images"] = "images"
    pages: list[PageRef]


class TextContent(SdkModel):
    kind: Literal["text"] = "text"
    xhtml: str  # the host sanitises it; reserved until a native text source exists
    assets: list[ImageRequest] = Field(default_factory=list)


class FileAsset(SdkModel):
    """Passthrough CBZ / EPUB / PDF: the bytes received are kept (EF-SRC-015)."""

    kind: Literal["file"] = "file"
    request: ImageRequest
    mime: str
    filename: str


UnitPayload = Annotated[ImagePages | TextContent | FileAsset, Field(discriminator="kind")]


class RangedPayload(SdkModel):
    """One file for a range of Units (EF-SRC-032)."""

    asset: FileAsset
    covers: list[str]  # remote_unit_id covered, in order -> unit_coverage
    bounds: dict[str, tuple[str, str]] = Field(default_factory=dict)  # id -> (start, end) locators
    volume_closed: bool  # closed volume: hash frozen; otherwise rolling tail (EF-BIB-040)
