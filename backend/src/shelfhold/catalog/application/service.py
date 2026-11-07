"""The catalog use cases, bound to their stores."""

from datetime import datetime
from decimal import Decimal

from shelfhold.catalog.application import contributors, editions, ingest, series, teams
from shelfhold.catalog.application.contributors import ContributorStore
from shelfhold.catalog.application.dto import (
    BindingRef,
    EditionRef,
    EditionSpec,
    IncomingUnit,
    IngestResult,
    TeamRef,
    UnitView,
)
from shelfhold.catalog.application.ports import EditionStore, TeamStore, UnitStore, WorkStore
from shelfhold.catalog.domain.identity import TextVersionRef
from shelfhold.catalog.domain.placement import (
    VariantFacts,
    choose_preferred,
    missing_numbers,
    place_owner,
)


class CatalogService:
    def __init__(
        self,
        works: WorkStore,
        edition_store: EditionStore,
        team_store: TeamStore,
        units: UnitStore,
        contributors: ContributorStore | None = None,
    ) -> None:
        self._contributors = contributors
        self._works = works
        self._editions = edition_store
        self._teams = team_store
        self._units = units

    # Series, Works, contributors
    def create_series(self, title: str) -> str:
        return self._works.insert_series(title)

    def create_work(
        self, kind: str, title: str, *, series_id: str | None = None, position: str = "1"
    ) -> str:
        """A Work stands alone; a Series above it is optional (EF-BIB-001)."""
        return self._works.insert_work(kind, title, series_id, position)

    def series_of(self, work_id: str) -> str | None:
        return self._works.series_of(work_id)

    def attach_to_series(self, series_id: str, work_id: str, position: str) -> None:
        """A Series that comes after the Work: only the membership is written (EF-BIB-025)."""
        series.attach_work(self._works, series_id, work_id, position)

    def create_contributor(self, name: str) -> str:
        return self._works.insert_contributor(name)

    def work_is_credited(self, work_id: str) -> bool:
        return self._contributors_store().credited(work_id)

    def credit_work(self, work_id: str, role: str, names: list[str]) -> int:
        """Credit the Work with the people a source names (EF-BIB-046); returns the new credits."""
        return contributors.record_credits(self._contributors_store(), work_id, role, names)

    def _contributors_store(self) -> ContributorStore:
        if self._contributors is None:
            raise RuntimeError("this catalog was built without a contributor store")
        return self._contributors

    # Editions
    def ensure_edition(self, spec: EditionSpec) -> EditionRef:
        return editions.ensure_edition(self._editions, spec)

    @staticmethod
    def translation_type_for(nature: str) -> str:
        return editions.translation_type_for(nature)

    def edition_for_translation(
        self,
        spec: EditionSpec,
        *,
        kind: str,
        nature: str,
        text_version: TextVersionRef | None = None,
    ) -> EditionRef:
        return editions.edition_for_translation(
            self._editions, spec, kind=kind, nature=nature, text_version=text_version
        )

    # Teams
    def resolve_team(self, binding: BindingRef, label: str, seen_at: datetime) -> TeamRef:
        return teams.resolve_team(self._teams, binding, label, seen_at)

    def set_team_priority(self, edition_id: str, team_ids: list[str]) -> None:
        teams.set_team_priority(self._teams, edition_id, team_ids)

    def team_priority(self, edition_id: str) -> list[str]:
        return self._teams.priority(edition_id)

    # Units and Variants
    def ingest(self, item: IncomingUnit) -> IngestResult:
        return ingest.ingest_unit(self._units, self._teams, item)

    def mark_removed(self, variant_id: str, at: datetime) -> None:
        self._units.mark_removed(variant_id, at)

    def place_owner(self, unit_id: str) -> str | None:
        return place_owner(self._units.facts_at(unit_id))

    def elect_preferred(self, unit_id: str) -> str | None:
        """The one Variant exported for the Unit: Team priority, then the oldest (I3)."""
        chosen = choose_preferred(self._units.facts_at(unit_id))
        self._units.set_preferred(unit_id, chosen)
        return chosen

    def variant_facts(self, unit_id: str) -> list[VariantFacts]:
        """What the election of the exported Variant needs to know of a Unit's Variants."""
        return self._units.facts_at(unit_id)

    def set_preferred(self, unit_id: str, variant_id: str | None) -> None:
        self._units.set_preferred(unit_id, variant_id)

    def units_in_order(self, edition_id: str) -> list[UnitView]:
        return self._units.units_in_order(edition_id)

    def units_beyond(self, edition_id: str, mark: tuple[int, Decimal]) -> list[str]:
        """Numbered Units strictly past the `(segment ordinal, number)` watermark (I34)."""
        return [
            unit_id
            for unit_id, _segment, ordinal, value in self._units.numbers(edition_id)
            if (ordinal, value) > mark
        ]

    def irreplaceable_candidates(self, edition_id: str) -> list[str]:
        return self._units.irreplaceable(edition_id)

    def missing_numbers(self, edition_id: str) -> list[tuple[str, str]]:
        return missing_numbers(
            (seg, value) for _u, seg, _o, value in self._units.numbers(edition_id)
        )
