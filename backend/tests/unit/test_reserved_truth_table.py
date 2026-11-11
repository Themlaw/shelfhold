"""ENF-SEC-010 / EF-USR-024: the exhaustive truth table `credential` x role x action.

The rows come from the registry of declared reserved actions plus one synthetic action per
`Effect`, so an action (or an effect) added later is covered without anyone remembering to. The
oracle below is written from the arbitrages (P3, P4), not by calling `authorize_reserved`.
"""

from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shelfhold.identity.public import (
    CREDENTIAL_AXIS,
    NO_REAUTH_EFFECTS,
    RESERVED_ACTIONS,
    TABLE_ROLES,
    Allowed,
    Credential,
    CredentialNotAllowed,
    Effect,
    Principal,
    ReauthRequired,
    ReservedAction,
    RoleNotAllowed,
    authorize_reserved,
    credential_axis,
    truth_table,
)

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
FRESH_S = 900
ROLES = ("admin", "member", "child")
# P3: irreversible or secret-revealing. Spelled out here on purpose (not imported).
P3 = {
    "deletes_permanently",
    "erases_reserved_dir",
    "touches_secrets",
    "confirms_restore_deletion",
    "touches_tombstones",
    "reinits_storage",
    "accepts_admin_risk",  # the admin password of Kavita goes through it (L38)
    "settles_fs_intent",  # `intents resolve` frees the paths of an interrupted operation (L51)
    "migrates_identity",  # `normalize migrate` rewrites the identity keys of live rows (L55)
    "acknowledges_restore",  # `restore acknowledge`: passkey logins come back with it
    "rewrites_exported_files",  # `revisions apply|force`: KOReader positions reset (M2 L17)
    "rates_content",  # fresh session in every profile (porteur, 2026-11-11): a stolen session
    "enrols_passkey",  # must not file adult content as "0" nor add a passkey that outlives it
}


def principal_for(axis: str, role: str = "admin") -> Principal:
    base = {"user_id": "u1", "username": "someone", "role": role, "can_rate": role == "admin"}
    match axis.split(":"):
        case ["fresh_session"]:
            return Principal(
                **base,
                credential=Credential.SESSION,
                session_id="s1",
                auth_time=T0 - timedelta(seconds=30),
            )
        case ["session"]:
            return Principal(
                **base,
                credential=Credential.SESSION,
                session_id="s1",
                auth_time=T0 - timedelta(hours=3),
            )
        case ["api_key", scope]:
            return Principal(**base, credential=Credential.API_KEY, key_id="k1", scopes=(scope,))
        case ["device_token"]:
            return Principal(**base, credential=Credential.DEVICE_TOKEN, key_id="d1")
        case ["local_cli"]:
            return Principal(**base, credential=Credential.LOCAL_CLI)
    raise AssertionError(axis)


# Everything else, by hand. A new `Effect` must be placed in one of the two sets or CI fails:
# the default of an unclassified effect is the safe one (freshness required).
NON_P3 = {
    "parental_control",
    "unmasks_request",
    "manages_credentials",
    "attests_trust",  # D15 (M2 L07)
}


@pytest.mark.req("EF-USR-024")
def test_every_effect_is_classed_p3_or_not_by_hand() -> None:
    assert {e.value for e in Effect} == P3 | NON_P3
    assert not (P3 & NON_P3)
    assert {Effect(v) for v in NON_P3} == NO_REAUTH_EFFECTS


@pytest.mark.req("EF-USR-024")
def test_an_unclassified_effect_would_require_freshness() -> None:
    stale = principal_for("session")
    action = ReservedAction("synthetic.new", frozenset({Effect.DELETES_PERMANENTLY}))
    assert isinstance(
        authorize_reserved(
            stale, action, now=T0, fresh_auth_s=FRESH_S, registry={action.name: action}
        ),
        ReauthRequired,
    )


def expected(
    axis: str, effects: frozenset[Effect], *, restricted: bool, hardened: bool, role: str = "admin"
) -> type[Allowed] | type[CredentialNotAllowed] | type[RoleNotAllowed] | type[ReauthRequired]:
    if axis not in ("local_cli", "fresh_session", "session"):
        return CredentialNotAllowed  # every key scope (admin included) and every device token
    if role != "admin":
        return RoleNotAllowed  # review PR #43 (M4): reserved means admin unless a route says so
    if Effect.ACKNOWLEDGES_RESTORE in effects and axis != "local_cli":
        return CredentialNotAllowed  # the host CLI only: no session, however fresh
    if axis == "local_cli":  # PR #49: the CLI is held to the role as well, never to freshness
        return Allowed
    needs_fresh = (
        hardened
        or bool({e.value for e in effects} & P3)
        or (Effect.PARENTAL_CONTROL in effects and restricted)
    )
    if axis == "fresh_session" or not needs_fresh:
        return Allowed
    return ReauthRequired


def synthetic_actions() -> list[ReservedAction]:
    return [ReservedAction(f"synthetic.{e.value}", frozenset({e})) for e in Effect]


ALL_ACTIONS = [*RESERVED_ACTIONS.values(), *synthetic_actions()]


@pytest.mark.req("EF-USR-024")
def test_axis_is_the_one_of_the_architecture() -> None:
    assert set(CREDENTIAL_AXIS) == {
        "fresh_session",
        "session",
        "api_key:read",
        "api_key:request",
        "api_key:admin",
        "device_token",
        "local_cli",
    }


@pytest.mark.req("EF-USR-024")
def test_registry_is_not_empty_and_every_effect_is_carried_by_a_declared_action() -> None:
    assert RESERVED_ACTIONS
    carried = set().union(*(a.effects for a in RESERVED_ACTIONS.values()))
    assert carried == set(Effect), (
        "an Effect no declared action uses: declare the action or drop it"
    )


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize("axis", CREDENTIAL_AXIS)
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("action", ALL_ACTIONS, ids=lambda a: a.name)
@pytest.mark.parametrize("restricted", [False, True])
@pytest.mark.parametrize("hardened", [False, True])
def test_truth_table_cell(
    axis: str, role: str, action: ReservedAction, restricted: bool, hardened: bool
) -> None:
    verdict = authorize_reserved(
        principal_for(axis, role),
        action,
        now=T0,
        fresh_auth_s=FRESH_S,
        restricted_profile_exists=restricted,
        hardened=hardened,
        registry={**RESERVED_ACTIONS, action.name: action},
    )
    assert isinstance(
        verdict,
        expected(axis, action.effects, restricted=restricted, hardened=hardened, role=role),
    )


@pytest.mark.req("EF-USR-024")
def test_a_route_may_name_the_other_roles_it_admits() -> None:
    action = RESERVED_ACTIONS["keys.create"]
    member = principal_for("fresh_session", "member")
    assert isinstance(authorize_reserved(member, action, now=T0), RoleNotAllowed)
    allowed = authorize_reserved(
        member, action, now=T0, allowed_roles=frozenset({"admin", "member"})
    )
    assert isinstance(allowed, Allowed)
    key = principal_for("api_key:admin", "member")  # a key is refused first, whatever the role
    assert isinstance(
        authorize_reserved(key, action, now=T0, allowed_roles=frozenset({"member"})),
        CredentialNotAllowed,
    )


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
@given(
    effects=st.frozensets(st.sampled_from(list(Effect)), min_size=1),
    axis=st.sampled_from(CREDENTIAL_AXIS),
    role=st.sampled_from(ROLES),
    restricted=st.booleans(),
    hardened=st.booleans(),
    fresh_s=st.integers(min_value=60, max_value=3600),
)
def test_truth_table_credential_axis_exhaustive(
    effects: frozenset[Effect], axis: str, role: str, restricted: bool, hardened: bool, fresh_s: int
) -> None:
    action = ReservedAction("synthetic.mixed", effects)
    principal = principal_for(axis, role)
    verdict = authorize_reserved(
        principal, action, now=T0, fresh_auth_s=fresh_s,
        restricted_profile_exists=restricted, hardened=hardened,
        registry={action.name: action},
    )  # fmt: skip
    # The "session" row is 3 h old, always beyond any `fresh_s` drawn here.
    assert isinstance(
        verdict, expected(axis, effects, restricted=restricted, hardened=hardened, role=role)
    )
    assert credential_axis(principal, now=T0, fresh_auth_s=fresh_s) == axis


@pytest.mark.req("EF-USR-024")
@pytest.mark.parametrize("axis", CREDENTIAL_AXIS)
def test_credential_axis_label_round_trips(axis: str) -> None:
    assert credential_axis(principal_for(axis), now=T0, fresh_auth_s=FRESH_S) == axis


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_table_covers_every_cell() -> None:
    cells = list(product(CREDENTIAL_AXIS, ROLES, ALL_ACTIONS))
    assert len(cells) == len(CREDENTIAL_AXIS) * len(ROLES) * len(ALL_ACTIONS)
    names = [a.name for a in ALL_ACTIONS]
    assert len(names) == len(set(names))


# --- the table generated from the registry, with its `fresh_auth_s` column (EF-USR-022, K5) ------

# One line per declared action, written by hand: "p3" (fresh by default), "parental" (fresh once a
# restricted profile exists) or "none" (fresh only in the hardened profile). An action added to
# `RESERVED_ACTIONS` with no line here fails the test below: a reserved action cannot ship without
# its row in the table.
FRESHNESS_LINE = {
    **dict.fromkeys(
        (
            "purge.now",
            "trash.empty",
            "unit.delete",
            "detach.full",
            "destination.remove_all",
            "reserved_dir.erase",
            "reconcile.confirm_deletion",
            "secrets.export",
            "secrets.rotate",
            "secrets.import",
            "source.set_secret",
            "target.rotate_key",
            "tombstone.lift",
            "tombstone.list",
            "tombstone.accept_lost_keyring",
            "tombstone.set_aside_journal",
            "tombstone.rekey",
            "root.takeover",
            "root.reinit",
            "store.takeover",
            "store.reinit",
            "config.path_map",
            "intents.resolve",
            "revisions.apply",
            "revisions.force",
            "normalize.migrate",
            "edition.merge",
            "restore.acknowledge",
            "admin_risk.accept",
            "shelf.purge_upstream",
        ),
        "p3",
    ),
    **dict.fromkeys(
        (
            "shelf.max_age_rating",
            "shelf.allowed_profiles",
            "shelf.choose_family",
            "shelf.withdraw",
            "profile.kavita_mapping",
            "target.recheck_child_exposure",
            "plugin.attest_partitioning",  # D15: which shelves a third-party target may serve
        ),
        "parental",
    ),
    **dict.fromkeys(
        (
            "rate",
            "request.unmask",
            "keys.create",
            "keys.rotate",
            "keys.revoke",
            "device_token.manage",
            "passkey.register",
            "passkey.revoke",
            "plugin.attest",  # D15: freshness only in the hardened profile
        ),
        "none",
    ),
}
PROFILES = list(product([False, True], [False, True]))  # (restricted, hardened)


def line_needs_fresh(name: str, *, restricted: bool, hardened: bool) -> bool:
    line = FRESHNESS_LINE[name]
    return hardened or line == "p3" or (line == "parental" and restricted)


@pytest.mark.req("EF-USR-022", "ENF-SEC-010")
def test_every_reserved_action_has_its_line_in_the_table() -> None:
    assert set(FRESHNESS_LINE) == set(RESERVED_ACTIONS), (
        "a reserved action without a line (or a line without its action)"
    )


@pytest.mark.req("EF-USR-022", "EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize(("restricted", "hardened"), PROFILES)
def test_generated_table_covers_every_action_role_and_credential(
    restricted: bool, hardened: bool
) -> None:
    rows = truth_table(
        now=T0, fresh_auth_s=FRESH_S, restricted_profile_exists=restricted, hardened=hardened
    )
    keys = [(r.action, r.role, r.credential) for r in rows]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(product(RESERVED_ACTIONS, TABLE_ROLES, CREDENTIAL_AXIS))
    for row in rows:
        want = expected(
            row.credential, RESERVED_ACTIONS[row.action].effects,
            restricted=restricted, hardened=hardened, role=row.role,
        )  # fmt: skip
        assert row.outcome == want.__name__, row
        needs = line_needs_fresh(row.action, restricted=restricted, hardened=hardened)
        # the column: the age a session must prove, whatever the verdict of this cell
        assert row.fresh_auth_s == (FRESH_S if needs else None), row


@pytest.mark.req("EF-USR-022", "ENF-SEC-010")
def test_generated_table_picks_up_an_action_added_later() -> None:
    extra = ReservedAction("synthetic.later", frozenset({Effect.DELETES_PERMANENTLY}))
    rows = truth_table(
        now=T0, fresh_auth_s=FRESH_S, registry={**RESERVED_ACTIONS, extra.name: extra}
    )
    later = [r for r in rows if r.action == "synthetic.later"]
    assert len(later) == len(TABLE_ROLES) * len(CREDENTIAL_AXIS)
    assert {r.fresh_auth_s for r in later} == {FRESH_S}


@pytest.mark.req("EF-USR-022", "ENF-SEC-010")
def test_table_roles_are_the_three_roles_and_the_integration_role() -> None:
    assert TABLE_ROLES == ("admin", "member", "child", "integration")
