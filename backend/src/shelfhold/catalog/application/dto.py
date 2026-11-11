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
    published_on: str | None = None  # partial ISO 8601 date, with its precision (EF-BIB-047)
    date_precision: str | None = None
    kind: str = "chapter"
    # What the source says of the Unit's availability, an open vocabulary (EF-BIB-009): None = it
    # said nothing; a value outside the contract is stored as `locked` with the text as detail.
    availability: str | None = None
    available_until: datetime | None = None
    # Metadata fields of the Unit that changed with this revision (field, new value): what a
    # revision that rewrites no file carries (EF-BIB-043). Only fields the Kavita API can take
    # are pushed; anything else goes to the "to rewrite" queue.
    metadata: tuple[tuple[str, str], ...] = ()


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
    suspect: str | None = None  # EF-BIB-024: why the revision was quarantined (stored one kept)
