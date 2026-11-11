"""EF-INT-023 (passe 13, N2): every exit command of a waiting state is classed, reserved or not."""

import pytest

from shelfhold.events.waiting_state import WaitingState
from shelfhold.identity.public import EXIT_ROUTES, EXIT_STATES_LATER, RESERVED_ACTIONS, Effect

# The classification of the specification, spelled out (not derived from the code under test).
RESERVED = {
    ("source set-secret", "auth_expired"),
    ("target rotate-key", "key_expiring"),
    ("secrets import", "tombstone_key_unknown"),
    ("tombstone accept-lost-keyring", "tombstone_key_unknown"),
    ("tombstone set-aside-journal", "restored_behind"),  # contradictor review, blocker 2
    ("tombstone set-aside-journal", "tombstone_journal_truncated"),  # M1b L26 (D12)
    ("rate", "rating_pending"),
    ("rate", "follow_suspended"),
    ("root takeover", "foreign_root"),
    ("root reinit", "foreign_root"),
    ("root takeover", "root_lost"),
    ("root reinit", "root_lost"),
    ("root takeover", "root_behind"),
    ("root reinit", "root_behind"),
    ("store reinit", "store_lost"),
    ("target recheck [child_exposure]", "child_exposure"),
    ("shelf withdraw", "withdrawal_pending"),  # M1b L10: EF-BIB-037
    ("restore acknowledge", "restored_credentials"),  # cross review: after a restore
    ("reconcile --adopt-after-restore [confirm-deletion]", "restored_behind"),
}
NOT_RESERVED = {
    ("missing resolve", "missing"),
    ("series resume", "paused_foreign_writer"),
    ("jobs list", "dead_letter"),
    ("jobs retry", "dead_letter"),
    ("target reconnect", "instance_changed"),
    ("target reconnect", "target_forbidden"),
    ("jobs retry", "not_indexed"),
    ("adopt --confirm-migration", "adoption_refused"),
    ("target recheck", "permission_denied"),
    ("target recheck", "route_missing"),
    ("source recheck", "auth_mode_unsupported"),
    ("adopt set-follow-from", "coverage_unknown"),
    ("edition set-lang", "language_unknown"),
    ("tombstone flush", "tombstone_lines_owed"),  # contradictor review, blocker 3
    ("target recheck", "mount_sentinel_missing"),  # L60: EF-EXP-029 in `status`
    ("edition set-series-name", "series_name_collision"),  # L60: EF-EXP-032
    ("reconcile --adopt-after-restore", "restored_behind"),
    ("jobs list", "safeguard_backlog"),
    ("root restored", "root_behind"),
    ("source reconnect", "source_instance_changed"),
}


@pytest.mark.req("EF-INT-023", "EF-USR-024")
def test_every_waiting_state_has_an_exit_route_with_its_class() -> None:
    covered = {route.waiting_state for route in EXIT_ROUTES}
    missing = {s.value for s in WaitingState} - covered - EXIT_STATES_LATER
    assert not missing, f"waiting states without a classified exit command: {sorted(missing)}"
    assert covered <= {s.value for s in WaitingState}
    assert not (covered & EXIT_STATES_LATER), "a state listed as later must not already have routes"


@pytest.mark.req("EF-INT-023", "EF-USR-024")
def test_exit_classification_matches_the_specification() -> None:
    reserved = {(r.command, r.waiting_state) for r in EXIT_ROUTES if r.reserved is not None}
    plain = {(r.command, r.waiting_state) for r in EXIT_ROUTES if r.reserved is None}
    assert reserved == RESERVED
    assert plain == NOT_RESERVED
    assert len(EXIT_ROUTES) == len(RESERVED) + len(NOT_RESERVED)  # no duplicate route


@pytest.mark.req("EF-INT-023", "EF-USR-024")
def test_reserved_exit_routes_are_declared_actions_with_an_effect() -> None:
    for route in EXIT_ROUTES:
        if route.reserved is not None:
            assert RESERVED_ACTIONS[route.reserved.name] is route.reserved
            assert route.reserved.effects


@pytest.mark.req("EF-USR-024")
def test_the_p3_actions_are_declared_with_the_effects_that_make_them_reserved() -> None:
    by_name = {name: action.effects for name, action in RESERVED_ACTIONS.items()}
    assert by_name["purge.now"] == {Effect.DELETES_PERMANENTLY}
    assert by_name["trash.empty"] == {Effect.DELETES_PERMANENTLY}
    assert by_name["detach.full"] == {Effect.DELETES_PERMANENTLY}
    assert Effect.TOUCHES_TOMBSTONES in by_name["tombstone.lift"]
    assert Effect.TOUCHES_TOMBSTONES in by_name["tombstone.list"]
    assert Effect.TOUCHES_SECRETS in by_name["secrets.export"]
    assert Effect.TOUCHES_SECRETS in by_name["secrets.rotate"]
    assert Effect.REINITS_STORAGE in by_name["store.reinit"]
    assert Effect.CONFIRMS_RESTORE_DELETION in by_name["reconcile.confirm_deletion"]
    assert Effect.PARENTAL_CONTROL in by_name["shelf.max_age_rating"]
    assert Effect.PARENTAL_CONTROL in by_name["shelf.allowed_profiles"]
    assert Effect.MANAGES_CREDENTIALS in by_name["keys.rotate"]
    assert Effect.ACCEPTS_ADMIN_RISK in by_name["admin_risk.accept"]
    assert Effect.UNMASKS_REQUEST in by_name["request.unmask"]
    assert Effect.RATES_CONTENT in by_name["rate"]
