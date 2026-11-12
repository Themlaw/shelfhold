"""EF-USR-005 / EF-USR-001 / ENF-SEC-010 (M3 L05): roles, the right to classify and the per-source
policy, as pure rules; plus the truth-table rows of the profile management."""

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.identity.public import (
    KNOWN_ROLES,
    RATE_ROLES,
    RESERVED_ACTIONS,
    Allowed,
    CredentialNotAllowed,
    Credential,
    Effect,
    Principal,
    ProfileError,
    ReauthRequired,
    RoleNotAllowed,
    authorize_reserved,
    holds_rate_right,
    truth_table,
)
from shelfhold.identity.domain.policy import (
    SOURCE_POLICIES,
    check_source_policy,
    default_source_policy,
    effective_source_policy,
)
from shelfhold.identity.domain.roles import check_child_ceiling, normalise_profile

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
PROFILE_ACTIONS = ("profile.create", "profile.update", "profile.remove", "profile.source_policy")


def session(role: str, *, fresh: bool, can_rate: bool = False) -> Principal:
    age = timedelta(seconds=30 if fresh else 3 * 3600)
    return Principal(
        user_id="u1", username="someone", role=role, credential=Credential.SESSION,
        session_id="s1", auth_time=T0 - age, can_rate=can_rate,
    )  # fmt: skip


@pytest.mark.req("EF-USR-005")
def test_roles_admin_member_child_policy_per_source() -> None:
    assert SOURCE_POLICIES == ("add_directly", "request", "forbidden")
    assert KNOWN_ROLES == {"admin", "member", "child"}
    # D14: a child asks on a family source and has nothing elsewhere; an adult adds directly
    assert default_source_policy("child", family=True) == "request"
    assert default_source_policy("child", family=False) == "forbidden"
    assert default_source_policy("member", family=False) == "add_directly"
    assert default_source_policy("admin", family=True) == "add_directly"
    # an explicit row wins for an adult, and for a child only where the source is a family one
    assert effective_source_policy("member", "forbidden", family=False) == "forbidden"
    assert effective_source_policy("child", "add_directly", family=True) == "add_directly"
    assert effective_source_policy("child", None, family=True) == "request"
    # a source that stops being a family one closes every door for a child, whatever the row says
    for stored in (*SOURCE_POLICIES, None):
        assert effective_source_policy("child", stored, family=False) == "forbidden"
    # an unknown role or policy is never admitted
    with pytest.raises(ProfileError):
        check_source_policy("ghost", "request", family=True)
    with pytest.raises(ProfileError):
        check_source_policy("member", "always", family=True)


@pytest.mark.req("EF-USR-005")
@pytest.mark.parametrize("policy", ["add_directly", "request"])
def test_child_policy_other_than_forbidden_refused_on_a_source_that_is_not_family(
    policy: str,
) -> None:
    with pytest.raises(ProfileError) as refused:
        check_source_policy("child", policy, family=False)
    assert refused.value.code == "child_source_not_family"
    check_source_policy("child", "forbidden", family=False)
    check_source_policy("child", policy, family=True)
    check_source_policy("member", policy, family=False)


@pytest.mark.req("EF-USR-005", "EF-USR-015")
def test_child_profile_requires_max_age() -> None:
    for missing in (None, -1, 100, True, "12", 12.5):
        with pytest.raises(ProfileError) as refused:
            normalise_profile("child", missing, can_rate=False)  # type: ignore[arg-type]
        assert refused.value.code == "child_requires_max_age", missing
    assert normalise_profile("child", 0, can_rate=False) == (0, False)  # 0 is the strictest, valid
    assert normalise_profile("child", 12, can_rate=False) == (12, False)
    for role in ("member", "admin"):  # an adult has no ceiling: giving one is refused, not ignored
        with pytest.raises(ProfileError) as refused:
            normalise_profile(role, 12, can_rate=False)
        assert refused.value.code == "max_age_only_for_child"
        assert normalise_profile(role, None, can_rate=False) == (None, False)
    with pytest.raises(ProfileError) as refused:
        normalise_profile("ghost", None, can_rate=False)
    assert refused.value.code == "invalid_role"


@pytest.mark.req("EF-USR-005", "EF-USR-015")
def test_a_ceiling_never_exceeds_the_one_of_who_creates_it() -> None:
    check_child_ceiling(None, 18)  # an adult without ceiling: any
    check_child_ceiling(12, 12)
    with pytest.raises(ProfileError) as refused:
        check_child_ceiling(12, 16)
    assert refused.value.code == "ceiling_above_creator"


@pytest.mark.req("EF-USR-005", "EF-BIB-036")
def test_child_never_gets_rate_right() -> None:
    assert {"admin", "member"} == RATE_ROLES  # the right is given per adult profile
    assert holds_rate_right("member", True)
    assert not holds_rate_right("member", False)
    for flag in (True, False):
        assert not holds_rate_right("child", flag)  # the flag alone never gives it
    with pytest.raises(ProfileError) as refused:
        normalise_profile("child", 10, can_rate=True)
    assert refused.value.code == "child_cannot_rate"
    assert normalise_profile("member", None, can_rate=True) == (None, True)
    # and a child that carries the flag anyway cannot classify
    verdict = authorize_reserved(
        session("child", fresh=True, can_rate=True), RESERVED_ACTIONS["rate"], now=T0,
        allowed_roles=frozenset({"admin", "member", "child"}),
    )  # fmt: skip
    assert isinstance(verdict, RoleNotAllowed)
    # a member who holds the right may classify when the route admits members
    allowed = authorize_reserved(
        session("member", fresh=True, can_rate=True), RESERVED_ACTIONS["rate"], now=T0,
        allowed_roles=frozenset({"admin", "member"}),
    )  # fmt: skip
    assert isinstance(allowed, Allowed)


@pytest.mark.req("EF-USR-022", "ENF-SEC-010")
def test_truth_table_profiles_rows_exhaustive() -> None:
    for name in PROFILE_ACTIONS:
        action = RESERVED_ACTIONS[name]
        assert action.effects == {Effect.MANAGES_PROFILES}
        assert action.needs_fresh_auth(restricted_profile_exists=False, hardened=False)
    for hardened in (False, True):
        for restricted in (False, True):
            rows = [
                r
                for r in truth_table(
                    now=T0, restricted_profile_exists=restricted, hardened=hardened
                )
                if r.action in PROFILE_ACTIONS
            ]
            assert len(rows) == len(PROFILE_ACTIONS) * 4 * 7  # action x role x credential axis
            for row in rows:
                assert row.fresh_auth_s == 900  # fresh in every profile, with no restricted child
                if row.role == "admin" and row.credential in ("fresh_session", "local_cli"):
                    assert row.outcome == "Allowed", row
                elif row.role == "admin" and row.credential == "session":
                    assert row.outcome == "ReauthRequired", row
                elif row.credential.startswith(("api_key", "device_token")):
                    assert row.outcome == "CredentialNotAllowed", row
                else:  # member, child, integration: never, fresh or not, host CLI included
                    assert row.outcome == "RoleNotAllowed", row


@pytest.mark.req("EF-USR-022", "ENF-SEC-010")
@pytest.mark.parametrize("name", PROFILE_ACTIONS)
def test_profile_actions_are_fresh_even_without_a_restricted_profile(name: str) -> None:
    action = RESERVED_ACTIONS[name]
    stale = authorize_reserved(session("admin", fresh=False), action, now=T0)
    assert isinstance(stale, ReauthRequired)
    assert isinstance(authorize_reserved(session("admin", fresh=True), action, now=T0), Allowed)
    for role in ("member", "child"):
        assert isinstance(
            authorize_reserved(session(role, fresh=True), action, now=T0), RoleNotAllowed
        )
    key = Principal(
        user_id="u1", username="k", role="admin", credential=Credential.API_KEY, key_id="k1",
        scopes=("admin",),
    )  # fmt: skip
    assert isinstance(authorize_reserved(key, action, now=T0), CredentialNotAllowed)
