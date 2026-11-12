"""Who may see what: the reader scope, and the `visibility` setting of a profile (EF-USR-006,
EF-USR-016).

`ViewerScope` is the one value that says which Works a reader sees. It is built from the role and
the ceiling of the profile that reads (`scope_of`), never from what a request claims, and the
catalogue turns it into a condition of the query (`catalog.application.queries`): nothing is
filtered after the fact, so a page, a count or a cursor cannot carry a hidden row out.

The rules are closed by default. An adult (admin, member) is unrestricted; a child sees what is
rated, not adult, and at most her ceiling; a profile the rules do not know, a child without a
valid ceiling, a missing profile, see nothing. A ceiling of 0 is the strictest, never "no ceiling".

`visibility` (`private` or `household`) is what a profile's own requests and activity show to the
others: `private` for an adult, `household` for a child (the parents see a child's requests).
"""

from dataclasses import dataclass
from typing import Final, Literal

from shelfhold.identity.domain.roles import MAX_AGE_LIMIT, ProfileError

VISIBILITIES: Final = ("private", "household")
ADULT_ROLES: Final = frozenset({"admin", "member"})
_KINDS: Final = ("all", "ceiling", "none")


@dataclass(frozen=True)
class ViewerScope:
    """`all`: an adult. `ceiling`: a child, with the highest minimum age she may see (99 is X).
    `none`: nobody the rules know."""

    kind: Literal["all", "ceiling", "none"]
    ceiling: int | None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS or (self.kind == "ceiling") != (self.ceiling is not None):
            raise ValueError("invalid reader scope")

    @property
    def unrestricted(self) -> bool:
        return self.kind == "all"


ALL: Final = ViewerScope("all", None)
NOBODY: Final = ViewerScope("none", None)


def _is_ceiling(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_AGE_LIMIT


def scope_of(role: object, max_age_rating: object) -> ViewerScope:
    """The scope of a profile of `role` with this ceiling; anything unknown or malformed sees
    nothing."""
    if role in ADULT_ROLES:
        return ALL
    if role == "child" and isinstance(max_age_rating, int) and _is_ceiling(max_age_rating):
        return ViewerScope("ceiling", max_age_rating)
    return NOBODY


def default_visibility(role: str) -> str:
    return "household" if role == "child" else "private"


def effective_visibility(role: str, stored: str | None) -> str:
    """What holds now: the choice of the profile, else the default of its role. A child is never
    hidden from the parents, whatever a row says; a value nobody defined is the default."""
    if role == "child":
        return "household"
    return stored if stored in VISIBILITIES else default_visibility(role)


def check_visibility(role: str, value: object) -> str:
    """`value` as a visibility for a profile of `role`, or a `ProfileError`."""
    if role not in ADULT_ROLES and role != "child":
        raise ProfileError("invalid_role")
    if not isinstance(value, str) or value not in VISIBILITIES:
        raise ProfileError("invalid_visibility")
    if role == "child" and value != "household":
        raise ProfileError("child_visibility_household")
    return value
