"""M3 L09: the reader scope and the `visibility` setting are pure rules, closed by default
(EF-USR-006, EF-USR-016).

Who sees what is decided by one value, the `ViewerScope`, built from the role and the ceiling of
the profile that reads. Anything the rules do not name sees nothing.
"""

import pytest

from shelfhold.identity.public import (
    VISIBILITIES,
    ProfileError,
    ViewerScope,
    check_visibility,
    default_visibility,
    effective_visibility,
    scope_of,
)


@pytest.mark.req("EF-USR-006")
def test_adults_see_everything_a_child_sees_up_to_the_ceiling() -> None:
    assert scope_of("admin", None).unrestricted
    assert scope_of("member", None).unrestricted
    child = scope_of("child", 12)
    assert not child.unrestricted and child.ceiling == 12 and child.kind == "ceiling"


@pytest.mark.req("EF-USR-006")
@pytest.mark.parametrize(
    ("role", "ceiling"),
    [
        ("child", None),  # a child with no ceiling is never "all ages"
        ("child", -1),
        ("child", 100),
        ("child", True),  # a bool is not a ceiling
        ("child", "12"),
        ("integration", None),
        ("ghost", 5),
        ("", None),
        (None, None),
    ],
)
def test_unknown_or_malformed_profile_sees_nothing(role: object, ceiling: object) -> None:
    scope = scope_of(role, ceiling)  # type: ignore[arg-type]
    assert scope.kind == "none" and not scope.unrestricted


@pytest.mark.req("EF-USR-006")
def test_scope_cannot_be_forged_by_hand() -> None:
    with pytest.raises(ValueError, match="scope"):
        ViewerScope("all", 5)  # an unrestricted scope carries no ceiling
    with pytest.raises(ValueError, match="scope"):
        ViewerScope("ceiling", None)
    with pytest.raises(ValueError, match="scope"):
        ViewerScope("whatever", None)


@pytest.mark.req("EF-USR-016")
def test_default_visibility_private_for_adults_household_for_children() -> None:
    assert VISIBILITIES == ("private", "household")
    assert default_visibility("admin") == default_visibility("member") == "private"
    assert default_visibility("child") == "household"
    assert effective_visibility("member", None) == "private"
    assert effective_visibility("member", "household") == "household"
    assert effective_visibility("child", None) == "household"
    assert effective_visibility("member", "bogus") == "private"  # closed: the role's default
    assert effective_visibility("child", "private") == "household"  # never hidden from parents


@pytest.mark.req("EF-USR-016")
def test_check_visibility_refuses_unknown_values_and_a_private_child() -> None:
    assert check_visibility("member", "household") == "household"
    assert check_visibility("admin", "private") == "private"
    assert check_visibility("child", "household") == "household"
    for bad in ("public", "", "ops", None, 3, "Private"):
        with pytest.raises(ProfileError) as refused:
            check_visibility("member", bad)  # type: ignore[arg-type]
        assert refused.value.code == "invalid_visibility"
    with pytest.raises(ProfileError) as child:
        check_visibility("child", "private")
    assert child.value.code == "child_visibility_household"
    with pytest.raises(ProfileError) as ghost:
        check_visibility("ghost", "private")
    assert ghost.value.code == "invalid_role"
