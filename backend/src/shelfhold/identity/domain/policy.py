"""What a profile may do with a source: add directly, ask, or nothing (EF-USR-005, D13, D14).

The defaults of D14 apply where no row exists. A child never gets more than a family source
offers: on a source that is not marked "family" (`source_config.family_safe`, D13) the only policy
a child can have is "forbidden", checked when the policy is set **and** every time it is read, so
that a source taken off the family list closes its door even if a row says otherwise.
"""

from typing import Final

from shelfhold.identity.domain.roles import KNOWN_ROLES, ProfileError, holds_rate_right

SOURCE_POLICIES: Final = ("add_directly", "request", "forbidden")


def default_source_policy(role: str, *, family: bool) -> str:
    """D14: a child asks on a family source and has nothing elsewhere; an adult adds directly (the
    shelf an adult files to, and the right to classify, are decided where the request is made)."""
    if role == "child":
        return "request" if family else "forbidden"
    return "add_directly"


def check_source_policy(role: str, policy: str, *, family: bool) -> None:
    if role not in KNOWN_ROLES:
        raise ProfileError("invalid_role")
    if policy not in SOURCE_POLICIES:
        raise ProfileError("invalid_policy")
    if role == "child" and not family and policy != "forbidden":
        raise ProfileError("child_source_not_family")


def effective_source_policy(role: str, stored: str | None, *, family: bool) -> str:
    """The policy that holds now: the explicit row if any, else the default; for a child on a
    source that is not a family one, always "forbidden"."""
    if role not in KNOWN_ROLES:
        return "forbidden"  # closed by default
    if role == "child" and not family:
        return "forbidden"
    return stored if stored in SOURCE_POLICIES else default_source_policy(role, family=family)


ADD_DIRECTLY_REFUSALS: Final = ("not_an_adult", "unrated_edition", "rate_right_required")


def add_directly_refusal(
    role: str, *, can_rate: bool, edition_rated: bool, shelf_family: bool
) -> str | None:
    """The row of the truth table "adult -> Ajouter directement -> shelf" (EF-BIB-055, D14), or
    None when nothing here stops it (the admission of the shelf still decides after).

    Closed by default: only an adult adds directly. Towards a family shelf (below 18+) an Edition
    nobody rated never goes, whoever the adult is (it waits in "À classer"); and an adult without
    the right to classify does not choose a family shelf at all, she asks. Towards any other shelf
    the filing is the adult's."""
    if role not in ("admin", "member"):
        return "not_an_adult"
    if not shelf_family:
        return None
    if not edition_rated:
        return "unrated_edition"
    if not holds_rate_right(role, can_rate):
        return "rate_right_required"
    return None
