"""The catalog use cases, bound to their stores."""

from datetime import datetime

from shelfhold.catalog.application import contributors, editions, ingest, series, teams
from shelfhold.catalog.application.contributors import ContributorStore
from shelfhold.catalog.application.dto import (
    BindingRef,
    EditionRef,
    EditionSpec,
    IncomingUnit,
    IngestResult,
    TeamRef,
)
from shelfhold.catalog.application.ports import EditionStore, TeamStore, UnitStore, WorkStore
from shelfhold.catalog.domain.identity import TextVersionRef
from shelfhold.catalog.domain.placement import (
    VariantFacts,
    missing_numbers,
)
from shelfhold.catalog.domain.revisions import DEFAULT_MIN_PAGE_RATIO


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

    def work_is_credited(self, work_id: str) -> bool:
        return self._contributors_store().credited(work_id)

    def credit_work(self, work_id: str, role: str, names: list[str]) -> int:
        """Credit the Work with the people a source names (EF-BIB-046); returns the new credits."""
        return contributors.record_credits(self._contributors_store(), work_id, role, names)

    def contributor(self, name: str) -> str | None:
        """The person a source names (made if new), for a text version or a death year."""
        return contributors.contributor_of(self._contributors_store(), name)

    def credit_edition(self, edition_id: str, contributor_id: str, role: str) -> bool:
        """Credit one Edition (a translator is the credit of a text version, not of the Work)."""
        return self._contributors_store().credit_edition(edition_id, contributor_id, role, 0)

    def set_death_year(self, contributor_id: str, year: int) -> bool:
        return self._contributors_store().set_death_year(contributor_id, year)

    def _contributors_store(self) -> ContributorStore:
        if self._contributors is None:
            raise RuntimeError("this catalog was built without a contributor store")
        return self._contributors

    # Editions
    def ensure_edition(self, spec: EditionSpec) -> EditionRef:
        return editions.ensure_edition(self._editions, spec)

    def edition_for_translation(
        self,
        spec: EditionSpec,
        *,
        kind: str,
        nature: str,
        text_version: TextVersionRef | None = None,
    ) -> EditionRef:
        """The Edition of a translation: a community one is a Team of one Edition, a published one
        a `text_version` (EF-BIB-049)."""
        return editions.edition_for_translation(
            self._editions, spec, kind=kind, nature=nature, text_version=text_version
        )

    @staticmethod
    def translation_type_for(nature: str) -> str:
        return editions.translation_type_for(nature)

    # Teams
    def resolve_team(self, binding: BindingRef, label: str, seen_at: datetime) -> TeamRef:
        return teams.resolve_team(self._teams, binding, label, seen_at)

    def set_team_priority(self, edition_id: str, team_ids: list[str]) -> None:
        teams.set_team_priority(self._teams, edition_id, team_ids)

    def prefer_team(self, edition_id: str, team_id: str) -> None:
        """The preferred team of the Edition: first of its priority list (EF-BIB-005)."""
        teams.prefer_team(self._teams, edition_id, team_id)

    def team_priority(self, edition_id: str) -> list[str]:
        return self._teams.priority(edition_id)

    # Units and Variants
    def ingest(
        self, item: IncomingUnit, *, min_page_ratio: float = DEFAULT_MIN_PAGE_RATIO
    ) -> IngestResult:
        return ingest.ingest_unit(self._units, self._teams, item, min_page_ratio=min_page_ratio)

    def variant_facts(self, unit_id: str) -> list[VariantFacts]:
        """What the election of the exported Variant needs to know of a Unit's Variants."""
        return self._units.facts_at(unit_id)

    def set_preferred(self, unit_id: str, variant_id: str | None) -> None:
        self._units.set_preferred(unit_id, variant_id)

    def missing_numbers(self, edition_id: str) -> list[tuple[str, str]]:
        return missing_numbers(
            (seg, value) for _u, seg, _o, value in self._units.numbers(edition_id)
        )
