"""Rights review of an Edition: who is credited, until when their rights may last, and the warning
(EF-SRC-024, EF-BIB-046, ADR-0032).

The calculation is `catalog.domain.public_domain` and it is indicative. Here the facts are
gathered (every credit of the Work and of the Edition with a role that carries a right of its own,
not only the author), the country of the instance is read (deduced from the locale until the admin
confirms it, and the calculation uses the unconfirmed value all the same, D4), and the warning is
announced. Announcing is all it does: `review` returns, and nothing it finds blocks an addition.
The `*_death_year` columns of the Edition are a cache derived from the credits.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from shelfhold.catalog.domain.public_domain import (
    TRANSLATION_ROLE,
    Holder,
    RightsAssessment,
    RightsWarning,
    Verdict,
    assess,
)

__all__ = [
    "EditionFacts",
    "Rights",
    "RightsReport",
    "RightsStore",
    "year_of",
]

WARNING_EVENT = "ops.rights_warning"
Publish = Callable[[str, Mapping[str, object]], object]


@dataclass(frozen=True)
class EditionFacts:
    edition_id: str
    work_id: str
    rights_basis: str | None
    first_published: str | None  # a partial date as stored: "1930", "1930-05", "1930-05-17"
    holders: tuple[Holder, ...]
    names: Mapping[str, str]  # contributor id -> primary name, for the translator cache


class RightsStore(Protocol):
    def facts(self, edition_id: str) -> EditionFacts | None: ...
    def jurisdiction(self) -> tuple[str, bool]: ...
    def candidates(self) -> list[str]: ...
    def write_cache(
        self,
        edition_id: str,
        author_death_year: int | None,
        translator: str | None,
        translator_death_year: int | None,
    ) -> None: ...


@dataclass(frozen=True)
class RightsReport:
    edition_id: str
    jurisdiction: str
    confirmed: bool
    verdict: Verdict
    warnings: tuple[RightsWarning, ...] = ()

    @property
    def reason(self) -> str | None:
        """The one code a list shows: the work's own warning before the translation's."""
        codes = [w.code for w in self.warnings]
        if not codes:
            return None
        return next((c for c in codes if c == "may_be_protected"), codes[0])


def year_of(partial_date: str | None) -> int | None:
    if not partial_date or len(partial_date) < 4 or not partial_date[:4].isdigit():
        return None
    return int(partial_date[:4])


class Rights:
    def __init__(self, store: RightsStore, publish: Publish | None = None) -> None:
        self._store = store
        self._publish = publish

    def jurisdiction(self) -> tuple[str, bool]:
        """`(country, confirmed)`: the country of the instance and whether the admin set it."""
        return self._store.jurisdiction()

    def assess(
        self,
        edition_id: str,
        *,
        today: date,
        died_for_france: frozenset[str] = frozenset(),
    ) -> RightsReport:
        """The calculation, without announcing anything."""
        facts = self._store.facts(edition_id)
        country, confirmed = self._store.jurisdiction()
        if facts is None:
            raise LookupError(edition_id)
        holders = [
            Holder(h.role, h.death_year,
                   True if h.contributor_id in died_for_france else h.died_for_france,
                   h.contributor_id)
            for h in facts.holders
        ]  # fmt: skip
        result: RightsAssessment = assess(
            country,
            holders,
            first_published_year=year_of(facts.first_published),
            today=today,
            rights_basis=facts.rights_basis,
        )
        return RightsReport(edition_id, country, confirmed, result.verdict, result.warnings)

    def review(
        self,
        edition_id: str,
        *,
        today: date,
        died_for_france: frozenset[str] = frozenset(),
    ) -> RightsReport:
        """`assess`, then the warning event when the work may still be protected. Never raises
        because of what it finds: the caller adds the book whatever the answer."""
        report = self.assess(edition_id, today=today, died_for_france=died_for_france)
        if report.reason is not None and self._publish is not None:
            self._publish(WARNING_EVENT, {"edition_id": edition_id, "reason": report.reason})
        return report

    def protected(self, today: date) -> list[RightsReport]:
        """Every Edition that may still be protected where the instance is (for `status`)."""
        found = (self.assess(edition_id, today=today) for edition_id in self._store.candidates())
        return [r for r in found if r.reason is not None]

    def refresh_cache(self, edition_id: str) -> None:
        """Derive `author_death_year`, `translator` and `translator_death_year` from the credits;
        a value the credits cannot give is left as the source wrote it."""
        facts = self._store.facts(edition_id)
        if facts is None:
            raise LookupError(edition_id)
        authors = [h.death_year for h in facts.holders if h.role == "aut" and h.death_year]
        translators = [h for h in facts.holders if h.role == TRANSLATION_ROLE]
        latest = max(translators, key=lambda h: h.death_year or 0, default=None)
        self._store.write_cache(
            edition_id,
            max(authors, default=None),
            facts.names.get(latest.contributor_id or "") if latest else None,
            latest.death_year if latest else None,
        )
