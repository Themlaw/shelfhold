"""Pure identity rules: session policy, attempt limiting, reserved actions (EF-USR-022, -024)."""

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.identity.public import (
    NO_REAUTH_EFFECTS,
    SYSTEM_EMITTERS,
    Allowed,
    Credential,
    CredentialNotAllowed,
    Effect,
    LoginThrottle,
    Principal,
    ReauthRequired,
    ReservedAction,
    RoleNotAllowed,
    authorize_reserved,
    session_policy,
)

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
ALL_EFFECTS = list(Effect)


def _principal(credential: Credential, *, auth_age: timedelta = timedelta(0)) -> Principal:
    return Principal(
        user_id="01JC0000000000000000000001",
        username="admin",
        role="admin",
        credential=credential,
        session_id="01JC0000000000000000000002" if credential is Credential.SESSION else None,
        auth_time=T0 - auth_age if credential is Credential.SESSION else None,
        key_id="01JC0000000000000000000003" if credential is Credential.API_KEY else None,
        scopes=("admin",) if credential is Credential.API_KEY else (),
    )


@pytest.mark.req("EF-USR-022")
@pytest.mark.parametrize("role", ["admin", "member", "child"])
def test_default_session_policy_is_30_days_sliding_for_every_role(role: str) -> None:
    policy = session_policy(role, hardened=False)
    assert policy.idle == timedelta(days=30)
    assert policy.absolute is None
    assert policy.remember_default is True


@pytest.mark.req("EF-USR-022")
def test_hardened_policy_shortens_admin_only() -> None:
    admin = session_policy("admin", hardened=True)
    assert admin.idle == timedelta(minutes=30)
    assert admin.absolute == timedelta(hours=12)
    assert session_policy("member", hardened=True) == session_policy("member", hardened=False)


@pytest.mark.req("ENF-SEC-008")
def test_throttle_ten_per_minute_per_address_then_backoff() -> None:
    throttle = LoginThrottle()
    for n in range(10):
        assert throttle.retry_after(ip="192.0.2.1", account="admin", now=T0) is None
        throttle.failure(ip="192.0.2.1", account="admin", now=T0 + timedelta(seconds=n))
    wait = throttle.retry_after(ip="192.0.2.1", account="admin", now=T0 + timedelta(seconds=10))
    assert wait is not None
    assert 0 < wait <= 60
    assert throttle.retry_after(ip="192.0.2.2", account="admin", now=T0) is None
    assert throttle.retry_after(ip="192.0.2.1", account="admin", now=T0 + timedelta(61)) is None


@pytest.mark.req("ENF-SEC-008")
def test_throttle_groups_ipv6_by_64_prefix() -> None:
    throttle = LoginThrottle()
    for n in range(10):
        throttle.failure(ip=f"2001:db8:1:2::{n + 1:x}", account=f"user{n}", now=T0)
    assert throttle.retry_after(ip="2001:db8:1:2::ffff", account="x", now=T0) is not None
    assert throttle.retry_after(ip="2001:db8:1:3::1", account="x", now=T0) is None


@pytest.mark.req("ENF-SEC-008")
def test_throttle_memory_is_bounded() -> None:
    throttle = LoginThrottle(max_keys=100)
    for n in range(1000):
        throttle.failure(ip=f"10.0.{n // 256}.{n % 256}", account=f"user{n}", now=T0)
    assert throttle.tracked_keys() <= 200


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize("effect", ALL_EFFECTS)
@pytest.mark.parametrize("credential", [Credential.API_KEY, Credential.DEVICE_TOKEN])
def test_reserved_actions_refused_to_keys_and_device_tokens(
    effect: Effect, credential: Credential
) -> None:
    action = ReservedAction(f"test.{effect.value}", frozenset({effect}))
    verdict = authorize_reserved(
        _principal(credential), action, now=T0, fresh_auth_s=900, restricted_profile_exists=True
    )
    assert isinstance(verdict, CredentialNotAllowed)


@pytest.mark.req("EF-USR-024")
@pytest.mark.parametrize("effect", ALL_EFFECTS)
def test_local_cli_and_fresh_session_may_run_every_reserved_action(effect: Effect) -> None:
    action = ReservedAction(f"test.{effect.value}", frozenset({effect}))
    for principal in (_principal(Credential.LOCAL_CLI), _principal(Credential.SESSION)):
        verdict = authorize_reserved(
            principal, action, now=T0, fresh_auth_s=900, restricted_profile_exists=True
        )
        assert isinstance(verdict, Allowed)


@pytest.mark.req("EF-USR-022")
def test_reauth_list_is_the_irreversible_or_secret_revealing_effects() -> None:
    assert (
        frozenset(
            {
                Effect.DELETES_PERMANENTLY,
                Effect.ERASES_RESERVED_DIR,
                Effect.TOUCHES_SECRETS,
                Effect.CONFIRMS_RESTORE_DELETION,
                Effect.TOUCHES_TOMBSTONES,  # review PR #41 (7): tombstone lift and list
                Effect.REINITS_STORAGE,  # root/store takeover and reinit
            }
        )
        == set(Effect) - NO_REAUTH_EFFECTS
    )
    stale = _principal(Credential.SESSION, auth_age=timedelta(minutes=20))
    for effect in ALL_EFFECTS:
        action = ReservedAction(f"test.{effect.value}", frozenset({effect}))
        verdict = authorize_reserved(stale, action, now=T0, fresh_auth_s=900)
        expected = ReauthRequired if effect not in NO_REAUTH_EFFECTS else Allowed
        assert isinstance(verdict, expected), effect


@pytest.mark.req("EF-USR-022", "EF-USR-024")
def test_parental_change_reauth_only_with_restricted_profile() -> None:
    action = ReservedAction("test.max_age_rating", frozenset({Effect.PARENTAL_CONTROL}))
    stale = _principal(Credential.SESSION, auth_age=timedelta(minutes=20))
    without = authorize_reserved(
        stale, action, now=T0, fresh_auth_s=900, restricted_profile_exists=False
    )
    assert isinstance(without, Allowed)
    verdict = authorize_reserved(
        stale, action, now=T0, fresh_auth_s=900, restricted_profile_exists=True
    )
    assert isinstance(verdict, ReauthRequired)
    assert verdict.auth_age_s == 1200
    assert verdict.max_age_s == 900
    fresh = _principal(Credential.SESSION, auth_age=timedelta(minutes=14))
    assert isinstance(
        authorize_reserved(fresh, action, now=T0, fresh_auth_s=900, restricted_profile_exists=True),
        Allowed,
    )


@pytest.mark.req("EF-USR-022")
def test_hardened_profile_extends_freshness_to_every_reserved_action() -> None:
    stale = _principal(Credential.SESSION, auth_age=timedelta(minutes=20))
    action = ReservedAction("test.rate", frozenset({Effect.RATES_CONTENT}))
    assert isinstance(authorize_reserved(stale, action, now=T0, fresh_auth_s=900), Allowed)
    assert isinstance(
        authorize_reserved(stale, action, now=T0, fresh_auth_s=900, hardened=True),
        ReauthRequired,
    )


@pytest.mark.req("EF-USR-024")
def test_reserved_action_needs_at_least_one_effect() -> None:
    with pytest.raises(ValueError, match="reserved_action_without_effect"):
        ReservedAction("test.nothing", frozenset())


@pytest.mark.req("EF-USR-024")
def test_system_emitters_closed_list() -> None:
    """Passe 13 (N2): the only emitters of deletions exempt from the reserved-action rule."""
    assert (
        frozenset(
            {
                "staging_orphan_cleanup",
                "probe_file_cleanup",
                "trash_retention_purge",
                "disk_guard_purge",
            }
        )
        == SYSTEM_EMITTERS
    )


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize("role", ["member", "child", "stranger"])
def test_local_cli_is_held_to_the_role_too(role: str) -> None:
    action = ReservedAction("test.purge", frozenset({Effect.DELETES_PERMANENTLY}))
    principal = Principal("u", "u", role, Credential.LOCAL_CLI)
    assert isinstance(authorize_reserved(principal, action, now=T0), RoleNotAllowed)
    admin = Principal("u", "u", "admin", Credential.LOCAL_CLI)
    assert isinstance(authorize_reserved(admin, action, now=T0), Allowed)


@pytest.mark.req("EF-USR-024")
def test_a_look_alike_credential_is_not_local_cli() -> None:
    class Impostor(str):
        def __eq__(self, other: object) -> bool:
            return True

        __hash__ = str.__hash__

    action = ReservedAction("test.purge", frozenset({Effect.DELETES_PERMANENTLY}))
    principal = Principal("u", "u", "admin", Impostor("local_cli"))  # type: ignore[arg-type]
    assert isinstance(authorize_reserved(principal, action, now=T0), CredentialNotAllowed)
