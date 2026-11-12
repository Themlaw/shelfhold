"""The roles and the right to classify (EF-USR-005, EF-BIB-036), closed by default.

A role the specification does not know is never admitted: not by the registry of reserved
actions, not by a route that lists it. `integration` is a viewer of events (webhooks, outbox:
GAP-7-16), never a person who signs in, so it is not a known role.

"Classer" (the `rate` action) is a right of its own, carried by the `can_rate` flag of the user.
Since M3 L05 the admin and the member hold it, per profile (the admin gives it to an adult, never
to a child): `RATE_ROLES` is the one line that says who may. The flag alone never gives it to
another role, and no route, command or database edit makes a child able to classify.

The rules of a profile (`normalise_profile`) are pure: a child always has an age ceiling and never
the right to classify, an adult has no ceiling. The application and the HTTP routes call them,
they do not repeat them.
"""

from typing import Final

KNOWN_ROLES: Final = frozenset({"admin", "member", "child"})
INTEGRATION_ROLE: Final = "integration"
RATE_ROLES: Final = frozenset({"admin", "member"})
MAX_ADMINS: Final = 2  # the local admin and an optional second one (EF-USR-001)
MAX_AGE_LIMIT: Final = 99  # 99 is "X": the highest ceiling an age rating can carry
# Roles a table lists: the three that sign in, then the one that only receives projections.
TABLE_ROLES: Final = ("admin", "member", "child", INTEGRATION_ROLE)


def holds_rate_right(role: str, can_rate: bool) -> bool:
    return role in RATE_ROLES and can_rate


PROFILE_ERRORS: Final = frozenset(
    {
        "not_allowed", "not_found", "invalid_role", "invalid_policy", "child_requires_max_age",
        "max_age_only_for_child", "child_cannot_rate", "child_source_not_family",
        "ceiling_above_creator", "username_taken", "too_many_admins", "last_admin",
        "cannot_remove_self", "cannot_change_own_role", "changed_meanwhile",
    }
)  # fmt: skip


class ProfileError(Exception):
    """A refusal of the rules of a profile; `code` is one of `PROFILE_ERRORS`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_ceiling(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_AGE_LIMIT


def normalise_profile(
    role: str, max_age_rating: int | None, *, can_rate: bool
) -> tuple[int | None, bool]:
    """The ceiling and the right to classify a profile of `role` may carry, or a refusal.

    A child needs an age ceiling (0 is the strictest, never "no ceiling") and never holds the
    right to classify; an adult has no ceiling, and giving one is refused rather than ignored."""
    if role not in KNOWN_ROLES:
        raise ProfileError("invalid_role")
    if role == "child":
        if not _is_ceiling(max_age_rating):
            raise ProfileError("child_requires_max_age")
        if can_rate:
            raise ProfileError("child_cannot_rate")
        return max_age_rating, False
    if max_age_rating is not None:
        raise ProfileError("max_age_only_for_child")
    return None, can_rate


def check_child_ceiling(creator_max_age: int | None, ceiling: int) -> None:
    """A ceiling given to a child never exceeds the one of who gives it (an adult with a ceiling
    of their own; an adult without one may give any)."""
    if creator_max_age is not None and ceiling > creator_max_age:
        raise ProfileError("ceiling_above_creator")
