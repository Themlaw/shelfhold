"""The closed list of Kavita roles a child's own key may hold (EF-USR-012, EF-USR-017)."""

from __future__ import annotations

import pytest

from shelfhold.export.domain.kavita_child_accounts import (
    CHILD_ACCOUNT_ADMIN,
    CHILD_ACCOUNT_PRIVILEGED,
    child_key_refusal,
)

# The role names of Kavita 0.9.1, as `PolicyConstants.cs` spells them; 5 is a Kavita age rating
# that a ceiling of 10 holds, as in test_destination_accounts_adverse.py.
EVERYDAY = ("Login", "Pleb", "Read Only", "Bookmark", "Download", "Change Password")


@pytest.mark.req("EF-USR-012", "EF-USR-017")
def test_a_child_account_with_the_everyday_roles_is_accepted() -> None:
    assert child_key_refusal(EVERYDAY, 5, False, 10) is None


@pytest.mark.req("EF-USR-012", "EF-USR-017")
@pytest.mark.parametrize("role", ["Change Restriction", "Promote", "A role of a later Kavita"])
def test_a_role_that_widens_the_account_is_refused(role: str) -> None:
    assert child_key_refusal((*EVERYDAY, role), 5, False, 10) == CHILD_ACCOUNT_PRIVILEGED


@pytest.mark.req("EF-USR-012", "EF-USR-017")
def test_an_administrator_account_is_refused() -> None:
    assert child_key_refusal(("Login", "Admin"), 5, False, 10) == CHILD_ACCOUNT_ADMIN
