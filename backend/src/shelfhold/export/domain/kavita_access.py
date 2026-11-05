"""Who can read a Kavita library: the read-only audit of EF-EXP-055 (and its K4 amendment).

A library is **exposed to children** when a non-administrator account reaches it with an age
restriction or with "include unknowns" switched off. The profile of a child (EF-USR-017) joins
the rule in M3. The rules here are pure; the reading and the pausing are in the adapter.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

NOT_APPLICABLE = -1  # AgeRating.NotApplicable: Kavita's value for "no restriction"


@dataclass(frozen=True)
class Account:
    username: str
    is_admin: bool
    age_rating: int  # AgeRating of the restriction; NOT_APPLICABLE when none
    include_unknowns: bool
    library_ids: frozenset[int]

    @property
    def age_restricted(self) -> bool:
        return self.age_rating != NOT_APPLICABLE


def reaches(account: Account, library_id: int) -> bool:
    return account.is_admin or library_id in account.library_ids


def is_child_like(account: Account) -> bool:
    """Not an administrator, with an age restriction or without the unknowns."""
    return not account.is_admin and (account.age_restricted or not account.include_unknowns)


def exposed_accounts(accounts: Iterable[Account], library_id: int) -> tuple[Account, ...]:
    """The accounts that make the library exposed to children, in a stable order."""
    found = [a for a in accounts if is_child_like(a) and reaches(a, library_id)]
    return tuple(sorted(found, key=lambda a: a.username))


def restricted_accounts(
    accounts: Iterable[Account], all_library_ids: frozenset[int]
) -> tuple[str, ...]:
    """Accounts that are restricted in any way: age, unknowns, or only some libraries (K4)."""
    return tuple(
        sorted(
            a.username
            for a in accounts
            if not a.is_admin and (is_child_like(a) or not all_library_ids <= a.library_ids)
        )
    )


# Kavita's AgeRating -> the age it stands for (X18Plus lets everything through).
_AGE_OF_RATING = {
    1: 0, 2: 3, 3: 6, 4: 6, 5: 10, 6: 10, 7: 12, 8: 13, 9: 15, 10: 17, 11: 17, 12: 18, 13: 18,
    14: 99,
}  # fmt: skip


def age_of_rating(rating: int) -> int | None:
    """The age a Kavita `AgeRating` stands for; None for "unknown", "pending", "not applicable"."""
    return _AGE_OF_RATING.get(rating) if rating > 1 else None


def tier(account: Account) -> int:
    """The highest class this account may read. A restriction without usable tier (only the
    unknowns switched off) counts as the lowest: it is what a child account looks like."""
    if account.age_restricted:
        return age_of_rating(account.age_rating) or 0
    return 0 if not account.include_unknowns else 99


def check_link(shelf_ceiling: int | None, exposed: tuple[Account, ...]) -> tuple[str, ...]:
    """The accounts that refuse a shelf: names, empty when the shelf may be linked.

    A shelf (no ceiling = X) may be linked when its ceiling does not exceed the tier of any
    exposed account. In M1a every shelf is 18+ or X, so an exposed library always refuses.
    """
    ceiling = 100 if shelf_ceiling is None else shelf_ceiling
    return tuple(a.username for a in exposed if ceiling > tier(a))
