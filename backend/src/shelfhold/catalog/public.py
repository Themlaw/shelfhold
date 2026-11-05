"""The catalog facade: the only door other contexts use (import-linter `contexts-independent`)."""

from collections.abc import Callable

import sqlalchemy as sa

from shelfhold.catalog.adapters.repo import (
    SqlEditionStore,
    SqlTeamStore,
    SqlUnitStore,
    SqlWorkStore,
)
from shelfhold.catalog.adapters.tx import run_immediate
from shelfhold.catalog.application.dto import (
    BindingRef,
    EditionRef,
    EditionSpec,
    IncomingUnit,
    IngestResult,
    TeamRef,
    UnitView,
)
from shelfhold.catalog.application.service import CatalogService
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
from shelfhold.catalog.domain.placement import IngestOutcome, VariantFacts


class Catalog(CatalogService):
    """The catalog on an open connection; the caller owns the transaction."""

    def __init__(self, connection: sa.Connection) -> None:
        super().__init__(
            SqlWorkStore(connection),
            SqlEditionStore(connection),
            SqlTeamStore(connection),
            SqlUnitStore(connection),
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


__all__ = [
    "TEXT_VERSION_LABELS",
    "BindingRef",
    "Catalog",
    "ContributorName",
    "Discriminant",
    "EditionRef",
    "EditionSpec",
    "IncomingUnit",
    "IngestOutcome",
    "IngestResult",
    "NameKind",
    "NoPersonFieldName",
    "PersonField",
    "TeamRef",
    "TextVersionRef",
    "UnitView",
    "VariantFacts",
    "ingest_isolated",
    "join_people",
    "person_field_name",
    "translation_discriminant",
]
