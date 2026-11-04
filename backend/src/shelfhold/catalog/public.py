"""The catalog facade: the only door other contexts use (import-linter `contexts-independent`)."""

import sqlalchemy as sa

from shelfhold.catalog.adapters.repo import (
    SqlEditionStore,
    SqlTeamStore,
    SqlUnitStore,
    SqlWorkStore,
)
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
from shelfhold.catalog.domain.placement import IngestOutcome


class Catalog(CatalogService):
    """The catalog on an open connection; the caller owns the transaction."""

    def __init__(self, connection: sa.Connection) -> None:
        super().__init__(
            SqlWorkStore(connection),
            SqlEditionStore(connection),
            SqlTeamStore(connection),
            SqlUnitStore(connection),
        )


__all__ = [
    "TEXT_VERSION_LABELS",
    "BindingRef",
    "Catalog",
    "Discriminant",
    "EditionRef",
    "EditionSpec",
    "IncomingUnit",
    "IngestOutcome",
    "IngestResult",
    "TeamRef",
    "TextVersionRef",
    "UnitView",
    "translation_discriminant",
]
