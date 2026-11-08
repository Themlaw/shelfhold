"""The age of a Work and of an Edition, and who may enter a shelf (EF-BIB-036, -037, -055).

Pure arithmetic over `AgeRating`: the maximum of the Editions, the explicit age of one Edition, the
ages that files and sources only suggest, and the rule of the Edition nobody has rated yet. A
suggestion is never a rating; a Work nobody rated is never under 18+.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from shelfhold.sdk.age import X_MIN_AGE, AgeRating, max_rating

UNRATED_FLOOR = AgeRating(X_MIN_AGE)  # what a Work nobody rated counts for, whatever it says
_X_CEILING = 99  # `shelf.max_age_rating` stores X as 99


def effective_age(work: AgeRating | None, editions: Iterable[AgeRating | None]) -> AgeRating | None:
    """The most restrictive of the Editions; the class of the Work itself is a floor.

    `None` when nobody gave an age: the Work is unrated (not an invented age).
    """
    return max_rating(a for a in (work, *editions) if a is not None)


def content_warning_age(warning: str) -> AgeRating | None:
    """The age a sub-source's `content_warning` suggests, never a rating: `nsfw` says X, `mixed`
    says 18+ (it may hold adult Works, so a shelf below 18+ waits for an admin), `safe` nothing."""
    if warning == "nsfw":
        return AgeRating(X_MIN_AGE, x=True)
    return AgeRating(X_MIN_AGE) if warning == "mixed" else None


def ceiling_of(max_age_rating: int | None) -> AgeRating | None:
    """`shelf.max_age_rating` as an age: `None` is no ceiling, 99 is X."""
    if max_age_rating is None:
        return None
    if max_age_rating >= _X_CEILING:
        return AgeRating(X_MIN_AGE, x=True)
    return AgeRating(max_age_rating)


class Admission(StrEnum):
    ALLOWED = "allowed"
    ABOVE_CEILING = "above_ceiling"  # the age is over the shelf's ceiling
    UNRATED_EDITION = "unrated_edition"  # new since the last rating: "À classer" (EF-BIB-055)
    SUGGESTION_ABOVE = "suggestion_above"  # a suggested age beats the rating (EF-BIB-055)


@dataclass(frozen=True)
class EditionAge:
    """What decides the admission of one Edition."""

    explicit: AgeRating | None  # fixed for this Edition by an admin
    rated: bool  # `edition.rated_at` is set
    work: AgeRating | None  # effective age of the Work
    suggested: tuple[AgeRating, ...]  # not refused, from files, sources, providers

    @property
    def age(self) -> AgeRating:
        """The Edition's own age when an admin fixed one, else the Work's; unrated counts 18+."""
        chosen = self.explicit if self.explicit is not None else self.work
        return UNRATED_FLOOR if chosen is None else chosen


def admission(edition: EditionAge, ceiling: AgeRating | None) -> Admission:
    """May this Edition enter a shelf whose maximum is `ceiling` (`None`: no ceiling)?

    Below 18+, an Edition nobody rated, or one a suggested age exceeds, stays out whatever the
    age of its Work says.
    """
    if ceiling is None:
        return Admission.ALLOWED
    guarded = ceiling < UNRATED_FLOOR
    if guarded and not edition.rated:
        return Admission.UNRATED_EDITION
    age = edition.age
    if not age.allowed_under(ceiling):
        return Admission.ABOVE_CEILING
    if guarded and any(age < s for s in edition.suggested):
        return Admission.SUGGESTION_ABOVE
    return Admission.ALLOWED


def parse_age(text: str) -> AgeRating:
    """`13+`, `13`, `0` (all audiences) or `X` as an age; `ValueError` for anything else. The tier
    itself is checked by `RatingService` (the scale of tiers is the service's)."""
    value = text.strip().upper().removesuffix("+")
    if value == "X":
        return AgeRating(X_MIN_AGE, x=True)
    if value.isascii() and value.isdigit():
        return AgeRating(int(value))
    raise ValueError(f"not an age: {text!r} (a tier such as 13+, or X)")
