"""M3 L09 (EF-BIB-055, ENF-SEC-010): the row "adult -> Ajouter directement -> shelf" of the truth
table, cell by cell. Closed by default: whatever the table does not allow, it refuses."""

import itertools

import pytest

from shelfhold.identity.public import ADD_DIRECTLY_REFUSALS, add_directly_refusal


def _oracle(role: str, can_rate: bool, rated: bool, family: bool) -> str | None:
    """Written by hand, from the rule of the card, not from the code."""
    if role not in ("admin", "member"):
        return "not_an_adult"
    if not family:
        return None  # an adult shelf takes what an adult chooses
    if not rated:
        return "unrated_edition"  # to every adult, whoever she is
    if role == "member" and not can_rate:
        return "rate_right_required"  # she asks instead (D14)
    return None


@pytest.mark.req("EF-BIB-055", "ENF-SEC-010")
@pytest.mark.parametrize(
    ("role", "can_rate", "rated", "family"),
    list(itertools.product(("admin", "member", "child", "integration", "ghost", ""), (True, False),
                           (True, False), (True, False))),
)  # fmt: skip
def test_add_directly_truth_table_cell(
    role: str, can_rate: bool, rated: bool, family: bool
) -> None:
    assert (
        add_directly_refusal(role, can_rate=can_rate, edition_rated=rated, shelf_family=family)
        == _oracle(role, can_rate, rated, family)
    )  # fmt: skip


@pytest.mark.req("EF-BIB-055")
def test_an_unrated_edition_never_reaches_a_family_shelf_for_any_adult() -> None:
    for role, can_rate in itertools.product(("admin", "member"), (True, False)):
        refusal = add_directly_refusal(
            role, can_rate=can_rate, edition_rated=False, shelf_family=True
        )
        assert refusal == "unrated_edition", (role, can_rate)
    assert set(ADD_DIRECTLY_REFUSALS) == {"not_an_adult", "unrated_edition", "rate_right_required"}
