"""What a child's Kavita account may reach, compared with the age of the child (EF-USR-017,
EF-EXP-036). Pure rules: the reading of Kavita and the storing of the answer are the adapter's.

A child's profile names its Kavita account (declarative, no token). For that account:

- the account must exist, and must not be an administrator (who reaches everything);
- it must reach **at least one family library**: a library fed only by shelves with an age
  ceiling below 18+; a library that no shelf feeds, or that an 18+ shelf feeds, is not family;
- it must reach **no other library** than the family ones, and no family library that holds
  above the child's age (the ceiling of the library is the highest ceiling of its shelves);
- it must carry an age restriction, no higher than the child's age, and not include the unknowns.

An account that nobody declared is never looked for by guessing: the child is `unmapped`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from shelfhold.export.domain.kavita_access import ADULT, Account, reaches, tier

OK = "ok"
ALERT = "alert"
UNMAPPED = "unmapped"

ACCOUNT_NOT_FOUND = "account_not_found"
ACCOUNT_IS_ADMIN = "account_is_admin"
NON_FAMILY_LIBRARY = "non_family_library"
LIBRARY_ABOVE_MAX_AGE = "library_above_max_age"
NO_FAMILY_LIBRARY = "no_family_library"
UNRESTRICTED = "unrestricted"
RESTRICTION_ABOVE_MAX_AGE = "restriction_above_max_age"
INCLUDES_UNKNOWNS = "includes_unknowns"

FINDING_CODES = frozenset(
    {
        ACCOUNT_NOT_FOUND, ACCOUNT_IS_ADMIN, NON_FAMILY_LIBRARY, LIBRARY_ABOVE_MAX_AGE,
        NO_FAMILY_LIBRARY, UNRESTRICTED, RESTRICTION_ABOVE_MAX_AGE, INCLUDES_UNKNOWNS,
    }
)  # fmt: skip


@dataclass(frozen=True)
class ChildAccount:
    """A child profile and the Kavita account declared for it: no title, no password."""

    profile_id: str
    max_age: int
    kavita_username: str | None


@dataclass(frozen=True)
class LibraryClass:
    """A library of Kavita and the highest age ceiling of the shelves that feed it.

    `ceiling` is None when no shelf feeds the library or one of them has no ceiling: a library
    nobody classed is never a family library."""

    ref: int
    ceiling: int | None

    @property
    def family(self) -> bool:
        return self.ceiling is not None and self.ceiling < ADULT


@dataclass(frozen=True)
class Finding:
    code: str
    library: str | None = None  # the remote reference of the library, when the finding has one


@dataclass(frozen=True)
class ChildReport:
    profile_id: str
    status: str
    findings: tuple[Finding, ...] = ()


def library_ceiling(ceilings: Iterable[int | None]) -> int | None:
    """The highest of the ceilings of the shelves of a library; None as soon as one has none."""
    seen = list(ceilings)
    if not seen or any(value is None for value in seen):
        return None
    return max(value for value in seen if value is not None)


def assess_child(
    child: ChildAccount, accounts: Iterable[Account], libraries: Sequence[LibraryClass]
) -> ChildReport:
    if child.kavita_username is None:
        return ChildReport(child.profile_id, UNMAPPED)
    wanted = child.kavita_username.casefold()
    account = next((a for a in accounts if a.username.casefold() == wanted), None)
    if account is None:
        return ChildReport(child.profile_id, ALERT, (Finding(ACCOUNT_NOT_FOUND),))
    findings: list[Finding] = []
    if account.is_admin:
        findings.append(Finding(ACCOUNT_IS_ADMIN))
    reached = [lib for lib in libraries if reaches(account, lib.ref)]
    findings += [Finding(NON_FAMILY_LIBRARY, str(lib.ref)) for lib in reached if not lib.family]
    family = [lib for lib in reached if lib.family]
    findings += [
        Finding(LIBRARY_ABOVE_MAX_AGE, str(lib.ref))
        for lib in family
        if lib.ceiling is not None and lib.ceiling > child.max_age
    ]
    if not family:
        findings.append(Finding(NO_FAMILY_LIBRARY))
    if not account.is_admin:  # an administrator has no restriction to read
        if not account.age_restricted:
            findings.append(Finding(UNRESTRICTED))
        elif tier(account) > child.max_age:
            findings.append(Finding(RESTRICTION_ABOVE_MAX_AGE))
        if account.include_unknowns:
            findings.append(Finding(INCLUDES_UNKNOWNS))
    return ChildReport(child.profile_id, ALERT if findings else OK, tuple(findings))
