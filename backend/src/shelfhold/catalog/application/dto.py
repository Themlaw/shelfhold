"""Values exchanged through the catalog facade: plain frozen data, no storage types."""

from dataclasses import dataclass
from datetime import datetime

from shelfhold.catalog.domain.identity import TextVersionRef
from shelfhold.catalog.domain.placement import IngestOutcome


@dataclass(frozen=True)
class BindingRef:
    """A source binding as acquisition knows it: its id and the Edition it serves."""

    id: str
    edition_id: str


@dataclass(frozen=True)
class EditionSpec:
    """The key of an Edition (ADR-0021 plus `performance` and `packaging`)."""

    work_id: str
    lang: str
    translation_type: str
    media: str
    publisher: str = ""
    text_version: TextVersionRef | None = None
    performance: str = ""
    packaging: str = ""


@dataclass(frozen=True)
class EditionRef:
    id: str
    work_id: str
    text_version: str
    created: bool


@dataclass(frozen=True)
class TeamRef:
    id: str
    edition_id: str
    label: str
    created: bool


@dataclass(frozen=True)
class IncomingUnit:
    """One release as a source reports it."""

    binding: BindingRef
    remote_unit_id: str
    raw_number: str | None
    seen_at: datetime
    source_order: int = 0
    team_label: str | None = None
    lang: str | None = None
    revision_hint: str | None = None
    content_hash: str | None = None
    page_count: int | None = None
    title: str | None = None
    published_at: datetime | None = None
    kind: str = "chapter"


@dataclass(frozen=True)
class IngestResult:
    outcome: IngestOutcome
    unit_id: str
    variant_id: str
    team_id: str | None
    revision: int
    unit_created: bool = False
    supersedes_variant_id: str | None = None
    suggestion_id: str | None = None


@dataclass(frozen=True)
class UnitView:
    id: str
    number_raw: str
    number_norm: str
    track: str
    segment: str
    part: str
    source_order: int
