"""What the catalog tests do with a `Catalog` that production never does.

These calls used to be methods of `CatalogService` and of its stores, called only by tests (M1b
L31, PLAN-M1b D10). They live here: a test builds a `SupportCatalog` where it built a `Catalog`.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa

from shelfhold.catalog.application.dto import EditionRef, EditionSpec
from shelfhold.catalog.application.editions import edition_for_translation
from shelfhold.catalog.domain.identity import TextVersionRef, sort_title
from shelfhold.catalog.public import Catalog
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from tests.support.placement import choose_preferred, place_owner

_TRACK_RANK = {track: rank for rank, track in enumerate(catalog.UNIT_TRACKS)}


@dataclass(frozen=True)
class UnitView:
    id: str
    number_raw: str
    number_norm: str
    track: str
    segment: str
    part: str
    source_order: int


class SupportCatalog(Catalog):
    def __init__(self, connection: sa.Connection) -> None:
        super().__init__(connection)
        self._c = connection

    def create_series(self, title: str) -> str:
        series_id = new_ulid()
        self._c.execute(
            sa.insert(catalog.series).values(
                id=series_id, title=title, sort_title=sort_title(title)
            )
        )
        return series_id

    def create_contributor(self, name: str) -> str:
        contributor_id = new_ulid()
        self._c.execute(sa.insert(catalog.contributor).values(id=contributor_id, primary_name=name))
        return contributor_id

    def edition_for_translation(
        self,
        spec: EditionSpec,
        *,
        kind: str,
        nature: str,
        text_version: TextVersionRef | None = None,
    ) -> EditionRef:
        return edition_for_translation(
            self._editions, spec, kind=kind, nature=nature, text_version=text_version
        )

    def mark_removed(self, variant_id: str, at: datetime) -> None:
        v = catalog.unit_variant
        self._c.execute(
            sa.update(v)
            .where(v.c.id == variant_id, v.c.removed_at.is_(None))
            .values(removed_at=at, preferred=False)  # a removed Variant is no longer exported
        )

    def place_owner(self, unit_id: str) -> str | None:
        return place_owner(self._units.facts_at(unit_id))

    def elect_preferred(self, unit_id: str) -> str | None:
        """The one Variant exported for the Unit: Team priority, then the oldest (I3)."""
        chosen = choose_preferred(self._units.facts_at(unit_id))
        self._units.set_preferred(unit_id, chosen)
        return chosen

    def units_in_order(self, edition_id: str) -> list[UnitView]:
        u = catalog.unit.c
        rows = self._c.execute(
            sa.select(
                u.id, u.number_raw, u.number_norm, u.track, u.number_segment, u.part,
                u.source_order, u.number_segment_ordinal,
            ).where(u.edition_id == edition_id)
        ).all()  # fmt: skip
        ordered = sorted(
            rows,
            key=lambda r: (
                _TRACK_RANK[r.track],
                r.number_segment_ordinal,
                Decimal(r.number_norm or "0"),
                int(r.part or 1),
                r.source_order,
                r.id,
            ),
        )
        return [
            UnitView(
                r.id, r.number_raw, r.number_norm, r.track, r.number_segment, r.part, r.source_order
            )
            for r in ordered
        ]

    def units_beyond(self, edition_id: str, mark: tuple[int, Decimal]) -> list[str]:
        """Numbered Units strictly past the `(segment ordinal, number)` watermark (I34)."""
        return [
            unit_id
            for unit_id, _segment, ordinal, value in self._units.numbers(edition_id)
            if (ordinal, value) > mark
        ]

    def irreplaceable_candidates(self, edition_id: str) -> list[str]:
        """Units whose every Variant is removed: the candidates EF-BIB-042 must not invent."""
        u, v = catalog.unit.c, catalog.unit_variant.c
        live = sa.exists().where(v.unit_id == u.id, v.removed_at.is_(None))
        any_variant = sa.exists().where(v.unit_id == u.id)
        return list(
            self._c.execute(
                sa.select(u.id).where(u.edition_id == edition_id, any_variant, ~live).order_by(u.id)
            ).scalars()
        )
