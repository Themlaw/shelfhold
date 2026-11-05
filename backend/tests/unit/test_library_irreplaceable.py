"""irreplaceable: computed, never stored; and never deleted without a typed confirmation."""

import pytest

from shelfhold.library.domain.irreplaceable import (
    ConfirmationRequiredError,
    VariantState,
    confirmation_phrase,
    is_irreplaceable,
    require_typed_confirmation,
)

ULID = "01JC0000000000000000000000"
LIVE = VariantState(binding_active=True, removed=False)


@pytest.mark.req("EF-BIB-031")
@pytest.mark.parametrize(
    ("variants", "availability", "expected"),
    [
        ([LIVE], "free", False),
        ([LIVE, VariantState(False, False)], "purchased", False),
        ([], "free", True),  # a manual import has no source at all
        ([VariantState(binding_active=False, removed=False)], "free", True),  # binding disabled
        ([VariantState(binding_active=True, removed=True)], "free", True),  # variant gone
        ([LIVE], "removed", True),  # the source says it is gone
    ],
)
def test_irreplaceable_is_no_live_source(
    variants: list[VariantState], availability: str, expected: bool
) -> None:
    assert is_irreplaceable(variants, availability) is expected


@pytest.mark.req("EF-BIB-031")
def test_typed_confirmation_is_required_only_for_irreplaceable() -> None:
    require_typed_confirmation(ULID, irreplaceable=False, typed=None)
    for typed in (None, "", "oui", ULID.lower(), ULID + " x", "DELETE"):
        with pytest.raises(ConfirmationRequiredError) as raised:
            require_typed_confirmation(ULID, irreplaceable=True, typed=typed)
        assert raised.value.code == "unit.confirmation_required"
    require_typed_confirmation(ULID, irreplaceable=True, typed=confirmation_phrase(ULID))
    require_typed_confirmation(ULID, irreplaceable=True, typed=f"  {confirmation_phrase(ULID)}\n")
