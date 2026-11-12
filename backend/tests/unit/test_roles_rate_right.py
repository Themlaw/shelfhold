"""EF-USR-005 / EF-BIB-036: the roles and the right to classify ("rate"), closed by default.

Before M3 only the admin holds the right. Whatever a route names in `roles=`, a role the
specification does not know, an action nobody declared and a user without the right are refused.
"""

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.identity.public import (
    KNOWN_ROLES,
    RATE_ROLES,
    RESERVED_ACTIONS,
    ActionNotDeclared,
    Allowed,
    Credential,
    Effect,
    Principal,
    ReservedAction,
    RoleNotAllowed,
    authorize_reserved,
    holds_rate_right,
)

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
RATE = RESERVED_ACTIONS["rate"]
EVERYONE = frozenset({"admin", "member", "child", "integration", "ghost"})


def session(role: str, *, can_rate: bool) -> Principal:
    return Principal(
        user_id="u1", username="someone", role=role, credential=Credential.SESSION,
        session_id="s1", auth_time=T0 - timedelta(seconds=5), can_rate=can_rate,
    )  # fmt: skip


@pytest.mark.req("EF-USR-005", "EF-BIB-036")
def test_rate_right_held_by_adult_profiles_only_since_m3_l05() -> None:
    assert {"admin", "member", "child"} == KNOWN_ROLES
    assert {"admin", "member"} == RATE_ROLES  # given per adult profile, never to a child
    for role in ("admin", "member"):
        assert holds_rate_right(role, True)
        assert not holds_rate_right(role, False)  # the flag is the right, the role alone is not
    for role in ("child", "integration", "ghost"):
        assert not holds_rate_right(role, True)  # a flag set by hand does not widen it


@pytest.mark.req("EF-USR-005", "EF-BIB-036")
@pytest.mark.parametrize("role", ["child", "integration", "ghost"])
def test_route_naming_a_role_cannot_give_it_the_right_to_rate(role: str) -> None:
    verdict = authorize_reserved(session(role, can_rate=True), RATE, now=T0, allowed_roles=EVERYONE)
    assert isinstance(verdict, RoleNotAllowed)


@pytest.mark.req("EF-USR-005", "EF-BIB-036")
def test_admin_without_the_flag_cannot_rate_but_the_flag_alone_suffices() -> None:
    assert isinstance(
        authorize_reserved(session("admin", can_rate=False), RATE, now=T0), RoleNotAllowed
    )
    assert isinstance(authorize_reserved(session("admin", can_rate=True), RATE, now=T0), Allowed)


@pytest.mark.req("EF-USR-005", "ENF-SEC-010")
def test_unknown_role_refused_for_every_action_even_if_a_route_lists_it() -> None:
    for action in RESERVED_ACTIONS.values():
        verdict = authorize_reserved(
            session("ghost", can_rate=True), action, now=T0, allowed_roles=EVERYONE
        )
        assert isinstance(verdict, RoleNotAllowed), action.name


@pytest.mark.req("ENF-SEC-010")
def test_unknown_action_refused_even_for_a_fresh_admin() -> None:
    admin = session("admin", can_rate=True)
    forged = ReservedAction("rate.everything", frozenset({Effect.RATES_CONTENT}))
    assert isinstance(authorize_reserved(admin, forged, now=T0), ActionNotDeclared)
    # same name, other effects: the registry's object is the only one that counts
    lookalike = ReservedAction("rate", frozenset({Effect.PARENTAL_CONTROL}))
    assert isinstance(authorize_reserved(admin, lookalike, now=T0), ActionNotDeclared)
    assert isinstance(authorize_reserved(admin, RATE, now=T0), Allowed)
