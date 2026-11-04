# SPDX-License-Identifier: MIT
"""Advised age (EF-EXP-011): integer minimum age plus a separate X flag.

The maximum, and the comparison with a shelf ceiling, are arithmetic; X is always the most
restrictive. The ComicInfo mapping is frozen (B4); one more tier is added without a migration (the
tier table only validates the shape, it is not a schema).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import total_ordering

AGE_TIERS: tuple[int, ...] = (0, 10, 12, 13, 16, 18)
X_MIN_AGE = 18


@total_ordering
@dataclass(frozen=True)
class AgeRating:
    """Integer minimum age (0 = all audiences) and X flag; "unrated" is `None` elsewhere."""

    min_age: int
    x: bool = False

    def __post_init__(self) -> None:
        if self.min_age < 0:
            raise ValueError(f"age must be >= 0, got {self.min_age}")
        if self.x and self.min_age != X_MIN_AGE:
            raise ValueError("age X implies the 18+ tier")

    def _rank(self) -> tuple[bool, int]:
        return (self.x, self.min_age)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, AgeRating):
            return NotImplemented
        return self._rank() < other._rank()

    def allowed_under(self, ceiling: "AgeRating") -> bool:
        """True if this age fits under the ceiling (`shelf.max_age_rating`)."""
        return self._rank() <= ceiling._rank()


def max_rating(ratings: Iterable[AgeRating]) -> AgeRating | None:
    """Most restrictive age (effective age of a Work); `None` when no age is given."""
    return max(ratings, default=None)


def to_columns(rating: AgeRating | None) -> tuple[int | None, bool]:
    """Stored values: `(age_rating, age_x)`."""
    if rating is None:
        return (None, False)
    return (rating.min_age, rating.x)


def label_key(rating: AgeRating | None) -> str:
    """i18n catalogue key of the label ("All audiences", "12+"...)."""
    if rating is None:
        return "age.unrated"
    if rating.x:
        return "age.x"
    if rating.min_age == 0:
        return "age.all"
    return f"age.min.{rating.min_age}"


# Frozen ComicInfo mapping table: the nearest value above (B4).
_COMICINFO_CEILINGS: tuple[tuple[int, str], ...] = (
    (0, "Everyone"),
    (10, "Everyone 10+"),
    (13, "Teen"),
    (16, "Mature 17+"),
)
_COMICINFO_ADULT = "Adults Only 18+"
_COMICINFO_X = "X18+"


def to_comicinfo(rating: AgeRating | None) -> str | None:
    """ComicInfo `AgeRating`; `None` (unrated) writes no value."""
    if rating is None:
        return None
    if rating.x:
        return _COMICINFO_X
    for ceiling, value in _COMICINFO_CEILINGS:
        if rating.min_age <= ceiling:
            return value
    return _COMICINFO_ADULT


_REVERSE: dict[str, AgeRating | None] = {
    **dict.fromkeys(("everyone", "g", "kids to adults"), AgeRating(0)),
    **dict.fromkeys(("everyone 10+", "pg"), AgeRating(10)),
    **dict.fromkeys(("pegi 12", "12+"), AgeRating(12)),
    "teen": AgeRating(13),
    **dict.fromkeys(("ma15+", "mature 17+", "m"), AgeRating(16)),
    **dict.fromkeys(("adults only 18+", "r18+"), AgeRating(18)),
    "x18+": AgeRating(18, x=True),
    **dict.fromkeys(("unknown", "rating pending"), None),
}


def from_comicinfo(value: str) -> AgeRating | None:
    """Reverse rule at adoption: a **suggested** age, never a rating.

    Unknown values give `None` (unrated).
    """
    return _REVERSE.get(value.strip().casefold())
