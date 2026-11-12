"""The indicative public-domain calculation (EF-SRC-024, EF-BIB-046, ADR-0032).

Pure arithmetic over what the catalog knows: the death years of the people credited on a Work or
an Edition, the year of first publication, the basis of the rights the source declared and the
country of the instance. The result is a hint for a human, never a legal opinion and never a lock:
nothing in this module refuses anything, and "no information" is said as such, never as "free".

Rules (indicative):

- `licence` (a CC BY-SA text, say), `user_supplied` and `worldwide_public_domain`: no warning.
- United States: the year that counts is the year of first publication, protected for 95 years.
- France: life + 70 years from every credited person with a right of their own (author,
  illustrator, translator, adapter...), the French war extensions (6 years 152 days for the First
  World War, 8 years 120 days for the Second) for a work published before the end of each war, and
  30 more years for an author who died for France. The latest end wins. "Died for France" is
  three-valued: unknown (`None`) is read on the cautious side when the person died in a war the
  status covers (1914-1918, 1939-1945), so the warning shows until the 30 years could be over.
- Any other country: life + 70 years, the most common term. Each country may differ: the
  warning says "may be", and the rule is one line of `_TERMS` away from being refined.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

__all__ = [
    "PROTECTING_ROLES",
    "TRANSLATION_ROLE",
    "Holder",
    "RightsAssessment",
    "RightsWarning",
    "Verdict",
    "assess",
    "protected_until",
]

LIFE_TERM = 70
US_TERM = 95
DIED_FOR_FRANCE_BONUS = 30
# Deaths the status "mort pour la France" can cover (the two World Wars); unknown outside them
# is read as "no".
_DIED_FOR_FRANCE_WARS = ((1914, 1918), (1939, 1945))
TRANSLATION_ROLE = "trl"
# MARC relator codes with a right of their own: author, illustrator, artist, translator, adapter,
# author of the preface, creator.
PROTECTING_ROLES = frozenset({"aut", "ill", "art", TRANSLATION_ROLE, "adp", "prf", "cre"})
_BASES_WITHOUT_WARNING = frozenset({"licence", "user_supplied", "worldwide_public_domain"})
# French war extensions: (published up to this year, years, days). A work still protected under
# the 50-year term of the time (death + 50 reaching the date of the law) gets the extension.
_FR_WARS = ((1920, 1919, 6, 152), (1941, 1948, 8, 120))


class Verdict(StrEnum):
    NO_WARNING = "no_warning"
    MAY_BE_PROTECTED = "may_be_protected"
    NO_INFORMATION = "no_information"


@dataclass(frozen=True)
class Holder:
    """A person credited with a role that carries a right of their own."""

    role: str
    death_year: int | None
    died_for_france: bool | None = None  # None = unknown
    contributor_id: str | None = None


@dataclass(frozen=True)
class RightsWarning:
    code: str  # may_be_protected | translation_may_be_protected
    role: str
    protected_until: date
    contributor_id: str | None = None


@dataclass(frozen=True)
class RightsAssessment:
    jurisdiction: str
    verdict: Verdict
    warnings: tuple[RightsWarning, ...] = ()


def _end_of(year: int) -> date:
    return date(year, 12, 31)


def _later(end: date, years: int, days: int = 0) -> date:
    try:
        moved = end.replace(year=end.year + years)
    except ValueError:  # 29 February
        moved = end.replace(year=end.year + years, day=28)
    return moved + timedelta(days=days)


def _could_have_died_for_france(death_year: int) -> bool:
    return any(first <= death_year <= last for first, last in _DIED_FOR_FRANCE_WARS)


def protected_until(
    jurisdiction: str,
    *,
    death_year: int,
    first_published_year: int | None,
    died_for_france: bool | None = False,
) -> date | None:
    """The last day the rights of one person may last in `jurisdiction`; `None` when the rule
    does not depend on the person (the United States)."""
    if jurisdiction.upper() == "US":
        return None
    # Published after the death is possible, but unknown is read as "before": it errs on the side
    # of the warning, which is only a hint.
    published = first_published_year if first_published_year is not None else death_year
    end = _end_of(death_year + LIFE_TERM)
    if jurisdiction.upper() == "FR":
        for last_publication, law_year, years, days in _FR_WARS:
            if published <= last_publication and death_year + 50 >= law_year:
                end = _later(end, years, days)
        if died_for_france or (died_for_france is None and _could_have_died_for_france(death_year)):
            end = _later(end, DIED_FOR_FRANCE_BONUS)
    return end


def assess(
    jurisdiction: str,
    holders: list[Holder],
    *,
    first_published_year: int | None,
    today: date,
    rights_basis: str | None = None,
) -> RightsAssessment:
    country = jurisdiction.upper()
    if rights_basis in _BASES_WITHOUT_WARNING:
        return RightsAssessment(country, Verdict.NO_WARNING)
    if country == "US":
        if first_published_year is None:
            return RightsAssessment(country, Verdict.NO_INFORMATION)
        end = _end_of(first_published_year + US_TERM)
        if end >= today:
            warning = RightsWarning("may_be_protected", "aut", end)
            return RightsAssessment(country, Verdict.MAY_BE_PROTECTED, (warning,))
        return RightsAssessment(country, Verdict.NO_WARNING)
    warnings: list[RightsWarning] = []
    unknown = False
    for holder in holders:
        if holder.role not in PROTECTING_ROLES:
            continue
        if holder.death_year is None:
            unknown = True
            continue
        ends = protected_until(
            country,
            death_year=holder.death_year,
            first_published_year=first_published_year,
            died_for_france=holder.died_for_france,
        )
        if ends is not None and ends >= today:
            code = (
                "translation_may_be_protected"
                if holder.role == TRANSLATION_ROLE
                else "may_be_protected"
            )
            warnings.append(RightsWarning(code, holder.role, ends, holder.contributor_id))
    if warnings:
        return RightsAssessment(country, Verdict.MAY_BE_PROTECTED, tuple(warnings))
    if unknown or not any(h.role in PROTECTING_ROLES for h in holders):
        return RightsAssessment(country, Verdict.NO_INFORMATION)
    return RightsAssessment(country, Verdict.NO_WARNING)
