# SPDX-License-Identifier: MIT
"""Âge conseillé (EF-EXP-011) : âge minimal entier et drapeau X séparé.

Le maximum et la comparaison avec le plafond d'une étagère sont arithmétiques ; X est toujours le
plus restrictif. La correspondance ComicInfo est figée (B4) ; un palier de plus s'ajoute sans
migration (la table des paliers n'est qu'une validation de forme, pas un schéma).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import total_ordering

AGE_TIERS: tuple[int, ...] = (0, 10, 12, 13, 16, 18)
X_MIN_AGE = 18


@total_ordering
@dataclass(frozen=True)
class AgeRating:
    """Âge minimal entier (0 = tous publics) et drapeau X ; « non classé » = `None` ailleurs."""

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
        """Vrai si cet âge tient sous le plafond (`shelf.max_age_rating`)."""
        return self._rank() <= ceiling._rank()


def max_rating(ratings: Iterable[AgeRating]) -> AgeRating | None:
    """Âge le plus restrictif (âge effectif d'une Œuvre) ; `None` si aucun âge n'est donné."""
    return max(ratings, default=None)


def to_columns(rating: AgeRating | None) -> tuple[int | None, bool]:
    """Valeurs stockées : `(age_rating, age_x)`."""
    if rating is None:
        return (None, False)
    return (rating.min_age, rating.x)


def label_key(rating: AgeRating | None) -> str:
    """Clé du catalogue i18n du libellé (« Tous publics », « 12+ »…)."""
    if rating is None:
        return "age.unrated"
    if rating.x:
        return "age.x"
    if rating.min_age == 0:
        return "age.all"
    return f"age.min.{rating.min_age}"


# Table de correspondance figée vers ComicInfo : valeur la plus proche supérieure (B4).
_COMICINFO_CEILINGS: tuple[tuple[int, str], ...] = (
    (0, "Everyone"),
    (10, "Everyone 10+"),
    (13, "Teen"),
    (16, "Mature 17+"),
)
_COMICINFO_ADULT = "Adults Only 18+"
_COMICINFO_X = "X18+"


def to_comicinfo(rating: AgeRating | None) -> str | None:
    """`AgeRating` ComicInfo ; `None` (non classé) n'écrit aucune valeur."""
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
    """Règle inverse à l'adoption : un âge **suggéré**, jamais un classement.

    Les valeurs inconnues donnent `None` (non classé).
    """
    return _REVERSE.get(value.strip().casefold())
