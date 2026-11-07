"""The catalog facade: the only door other contexts use (import-linter `contexts-independent`)."""

from collections.abc import Callable
from datetime import datetime

import sqlalchemy as sa

from shelfhold.catalog.adapters.repo import (
    SqlContributorStore,
    SqlEditionStore,
    SqlTeamStore,
    SqlUnitStore,
    SqlWorkStore,
)
from shelfhold.catalog.adapters.tx import run_immediate
from shelfhold.catalog.application.contributors import ARTIST, WRITER
from shelfhold.catalog.application.dating import period_start, stored_date
from shelfhold.catalog.application.dto import (
    BindingRef,
    EditionRef,
    EditionSpec,
    IncomingUnit,
    IngestResult,
    TeamRef,
    UnitView,
)
from shelfhold.catalog.application.editions import translation_type_for
from shelfhold.catalog.application.places import place_key
from shelfhold.catalog.application.rating import (
    RatingRefusedError,
    RatingResult,
    RatingService,
    Withdrawal,
)
from shelfhold.catalog.application.relink import (
    ListedUnit,
    Relinker,
    RelinkRefusedError,
    RelinkRequest,
    RelinkResult,
)
from shelfhold.catalog.application.series import SeriesAlreadyAttachedError
from shelfhold.catalog.application.service import CatalogService
from shelfhold.catalog.domain.age import (
    Admission,
    EditionAge,
    admission,
    ceiling_of,
    content_warning_age,
    effective_age,
)
from shelfhold.catalog.domain.dates import parse_partial_date
from shelfhold.catalog.domain.identity import (
    TEXT_VERSION_LABELS,
    Discriminant,
    TextVersionRef,
    translation_discriminant,
)
from shelfhold.catalog.domain.names import (
    ContributorName,
    NameKind,
    NoPersonFieldName,
    PersonField,
    join_people,
    person_field_name,
)
from shelfhold.catalog.domain.normalizers import (
    NormalizerId,
    NormalizerRegistry,
    default_registry,
)
from shelfhold.catalog.domain.numbering import NumberError, parse_number
from shelfhold.catalog.domain.placement import IngestOutcome, VariantFacts, place_of
from shelfhold.catalog.lang import LANG_NORMALIZER_ID, LanguageError, edition_key_lang
from shelfhold.identity.public import Principal


class Catalog(CatalogService):
    """The catalog on an open connection; the caller owns the transaction."""

    def __init__(self, connection: sa.Connection) -> None:
        super().__init__(
            SqlWorkStore(connection),
            SqlEditionStore(connection),
            SqlTeamStore(connection),
            SqlUnitStore(connection),
            SqlContributorStore(connection),
        )


class RelinkService(Relinker):
    """`relink` over the catalog of an open connection (EF-BIB-029)."""

    def __init__(self, rating: RatingService | None = None) -> None:
        super().__init__(Catalog, rating or RatingService())


def relink_isolated(
    engine: sa.Engine, request: RelinkRequest, *, principal: Principal, now: datetime
) -> RelinkResult:
    """`relink` in its own `BEGIN IMMEDIATE` transaction, audit entry included."""
    return run_immediate(
        engine, lambda c: RelinkService().relink(c, request, principal=principal, now=now)
    )


def ingest_isolated(
    engine: sa.Engine,
    item: IncomingUnit,
    *,
    then: Callable[[sa.Connection, IngestResult], None] | None = None,
) -> IngestResult:
    """Ingest one release in its own `BEGIN IMMEDIATE` transaction, replayed once on a clash.

    `then` runs in the same transaction, after the ingest: what must not exist without the
    Variant (a download ticket) commits or rolls back with it. It must be idempotent (a replay).
    """

    def work(connection: sa.Connection) -> IngestResult:
        result = Catalog(connection).ingest(item)
        if then is not None:
            then(connection, result)
        return result

    return run_immediate(engine, work)


def place_parts(raw_number: str | None, source_order: int = 0) -> tuple[str, str, str]:
    """(track, segment, part) of the place a release takes, as the ingest would file it: what a
    `place` tombstone is compared with (EF-BIB-053)."""
    try:
        number = parse_number(raw_number, source_order=source_order)
    except NumberError:
        number = parse_number(None, source_order=source_order)
    place = place_of(number)
    return place.track, place.segment, place.part


def numbered_mark(raw_number: str | None, source_order: int = 0) -> tuple[str, str] | None:
    """`(segment, number_norm)` of a numbered release as the ingest would file it, `None` when it
    is unnumbered: what a follow watermark is made of (EF-EXP-053)."""
    try:
        number = parse_number(raw_number, source_order=source_order)
    except NumberError:
        return None
    place = place_of(number)
    return None if place.unnumbered else (place.segment, place.number_norm)


__all__ = [
    "ARTIST",
    "LANG_NORMALIZER_ID",
    "TEXT_VERSION_LABELS",
    "WRITER",
    "Admission",
    "BindingRef",
    "Catalog",
    "ContributorName",
    "Discriminant",
    "EditionAge",
    "EditionRef",
    "EditionSpec",
    "IncomingUnit",
    "IngestOutcome",
    "IngestResult",
    "LanguageError",
    "ListedUnit",
    "NameKind",
    "NoPersonFieldName",
    "NormalizerId",
    "NormalizerRegistry",
    "PersonField",
    "RatingRefusedError",
    "RatingResult",
    "RatingService",
    "RelinkRefusedError",
    "RelinkRequest",
    "RelinkResult",
    "RelinkService",
    "SeriesAlreadyAttachedError",
    "TeamRef",
    "TextVersionRef",
    "UnitView",
    "VariantFacts",
    "Withdrawal",
    "admission",
    "ceiling_of",
    "content_warning_age",
    "default_registry",
    "edition_key_lang",
    "effective_age",
    "ingest_isolated",
    "join_people",
    "numbered_mark",
    "parse_number",
    "parse_partial_date",
    "period_start",
    "person_field_name",
    "place_key",
    "place_parts",
    "relink_isolated",
    "stored_date",
    "translation_discriminant",
    "translation_type_for",
]
