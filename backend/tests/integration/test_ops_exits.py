"""EF-INT-023 (L36): every waiting state has an exit command, played end to end.

Each M1a state is put in its state the way the product does (a pause in the database, a health
document, a job gone dead), seen by `status` with its reason and its command, then left by the
command the specification names, through the services of the lot that owns the state. The
enumeration is partitioned (M1a / later): a new `WaitingState` fails here until it is classed.
"""
# ruff: noqa: S105 (synthetic secrets)

import argparse
import inspect
from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.events.waiting_state import WaitingState
from shelfhold.export.application.preflight import PermissionPreflight
from shelfhold.export.application.target_state import load_health
from shelfhold.identity.public import EXIT_ROUTES, EXIT_STATES_LATER
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.interfaces.cli.registry import build_parser
from shelfhold.ops import CLI_PENDING, ENDPOINTS, M1A_STATES, OpsServices, bare, endpoint_of
from tests.export_env import ExportEnv, export_env
from tests.headless_env import KAVITA_URL, SUWAYOMI_URL, Headless, headless
from tests.kavita_export_env import wire
from tests.ops_env import (
    LOCAL_CLI,
    FakeAdoption,
    FakeKeyring,
    FakeRestore,
    add_job,
    edition_lang,
    job_status,
    make_ops,
    pause_follow,
    put_health,
)

pytestmark = pytest.mark.req("EF-INT-023")

LATER_STATES = {s for s in WaitingState if s.value in EXIT_STATES_LATER} | {
    s for s in WaitingState if s not in M1A_STATES and s.value not in EXIT_STATES_LATER
}


def _waiting(ops: OpsServices, state: str) -> list[object]:
    return [item for item in ops.status().waiting if item.state == state]


def _listed_then_gone(ops: OpsServices, state: str, leave: Callable[[], object]) -> None:
    """The state is listed with a reason and a command, `leave` is run, the state is gone."""
    before = _waiting(ops, state)
    assert before, f"{state} is not listed by status"
    assert all(item.reason and item.command.startswith("shelfhold ") for item in before)  # type: ignore[attr-defined]
    leave()
    assert _waiting(ops, state) == [], f"{state} is still listed after its exit command"


def _exported(env: ExportEnv, count: int = 1) -> tuple[str, list[str]]:
    series = env.new_series()
    for number in range(2, 2 + count):
        env.add_chapter(series, str(number))
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    return series.edition_id, [r.export_item_id for r in env.rows(export.export_record)]


def _cli_has(command: str) -> bool:
    """Does `shelfhold <command>` exist in the parser? A `--flag` is an option of the group."""
    words = bare(command).split()
    parser = build_parser()
    for index, word in enumerate(words):
        if word.startswith("--"):
            return any(word in action.option_strings for action in parser._actions)
        sub = next(
            (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None
        )
        if sub is None or word not in sub.choices:
            return False
        parser = sub.choices[word]
    return True


def _route_documented(command: str) -> bool:
    from fastapi.testclient import TestClient  # noqa: F401  (the app is built in `_openapi`)

    method, path = endpoint_of(command)
    return (method.lower(), path) in _openapi_paths()


_PATHS: set[tuple[str, str]] = set()


def _openapi_paths() -> set[tuple[str, str]]:
    if not _PATHS:
        from shelfhold.main import create_app

        spec = create_app().openapi()
        _PATHS.update((m, p) for p, ops in spec["paths"].items() for m in ops)
    return _PATHS


# -- the scenarios: one per M1a state ---------------------------------------------------------------


def missing() -> None:
    with export_env() as env:
        _edition, items = _exported(env)
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.export_record).values(state="missing"))
        ops = make_ops(env)
        _listed_then_gone(
            ops, "missing", lambda: ops.missing_resolve(
                env.destination_id, items[0], "reexport", actor_id=None
            ),
        )  # fmt: skip
        assert env.rows(export.export_record)[0].state == "planned"  # the next export puts it back


def paused_foreign_writer() -> None:
    with export_env() as env:
        _exported(env)
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.export_series).values(paused_reason="foreign_writer"))
        ops = make_ops(env)
        series_id = env.rows(export.export_series)[0].id
        _listed_then_gone(ops, "paused_foreign_writer", lambda: ops.series_resume(series_id))


def dead_letter() -> None:
    with export_env() as env:
        job = add_job(env.engine, kind="library.safeguard", status="dead_letter", code="job.failed")
        ops = make_ops(env)
        _listed_then_gone(ops, "dead_letter", lambda: ops.jobs_retry(job))
        assert job_status(env.engine, job) == "queued"


def instance_changed() -> None:
    with export_env() as env:
        w = wire(env)
        w.fake.reinstall("another-install")
        ops = make_ops(env, w)
        put_health(env, instance_changed=True, install_id="old")
        _listed_then_gone(ops, "instance_changed", lambda: ops.target_reconnect(env.target_id))


def target_forbidden() -> None:
    with export_env() as env:
        w = wire(env)
        ops = make_ops(env, w)
        put_health(env, paused=True, paused_route="POST /api/library/scan-folder")
        _listed_then_gone(ops, "target_forbidden", lambda: ops.target_reconnect(env.target_id))
        assert not load_health(env.engine.connect(), env.target_id).paused


def route_missing() -> None:
    with export_env() as env:
        w = wire(env)
        ops = make_ops(env, w)
        put_health(env, missing_routes=("POST /api/library/scan-folder",))
        _listed_then_gone(ops, "route_missing", lambda: ops.target_recheck(env.target_id))


def not_indexed() -> None:
    with export_env() as env:
        job = add_job(env.engine, kind="target.verify_indexed", status="dead_letter", code="job.failed")
        put_health(env, not_indexed=True)
        ops = make_ops(env)
        _listed_then_gone(ops, "not_indexed", lambda: ops.jobs_retry(job))


def child_exposure() -> None:
    with export_env() as env:
        w = wire(env)
        ops = make_ops(env, w)
        put_health(env, child_exposure=True)
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.shelf).values(paused_reason="child_exposure"))
        assert ops.target_recheck_is_reserved(env.target_id)  # the route asks for a fresh session
        _listed_then_gone(ops, "child_exposure", lambda: ops.target_recheck(env.target_id))
        assert env.rows(export.shelf)[0].paused_reason is None


def permission_denied() -> None:
    with export_env() as env:
        _exported(env)
        series_id = env.rows(export.export_series)[0].id
        PermissionPreflight(env.engine, env.lib.fsops, make_ops(env)._bus, clock=env.clock).mark_denied(
            env.destination_id, series_id
        )
        ops = make_ops(env)
        _listed_then_gone(ops, "permission_denied", lambda: ops.target_recheck(env.target_id))


def language_unknown() -> None:
    with export_env() as env:
        series = env.new_series(lang="x-unknown")
        ops = make_ops(env)
        _listed_then_gone(
            ops, "language_unknown", lambda: ops.edition_set_lang(series.edition_id, "ja")
        )
        assert edition_lang(env.engine, series.edition_id) == "ja"


def restored_behind() -> None:
    with export_env() as env:
        restore = FakeRestore()
        ops = make_ops(env, restore=restore)
        _listed_then_gone(
            ops, "restored_behind",
            lambda: ops.restore_adopt(confirm_deletion=False, principal=LOCAL_CLI),
        )  # fmt: skip
        assert restore.calls == [False]


def tombstone_key_unknown() -> None:
    with export_env() as env:
        keyring = FakeKeyring()
        ops = make_ops(env, keyring=keyring)
        _listed_then_gone(
            ops, "tombstone_key_unknown",
            lambda: ops.tombstone_accept_lost_keyring(
                confirmation="the keyring is lost", principal=LOCAL_CLI
            ),
        )  # fmt: skip
        assert keyring.waived == [("the keyring is lost", "local_cli")]


def adoption_refused() -> None:
    with export_env() as env:
        adoption = FakeAdoption()
        ops = make_ops(env, adoption=adoption)
        _listed_then_gone(
            ops, "adoption_refused",
            lambda: ops.adopt_confirm_migration(destination_id=env.destination_id, fingerprint="f"),
        )  # fmt: skip


def coverage_unknown() -> None:
    with export_env() as env:
        adoption = FakeAdoption()
        ops = make_ops(env, adoption=adoption)
        _listed_then_gone(
            ops, "coverage_unknown",
            lambda: ops.adopt_set_follow_from(
                destination_id=env.destination_id, series="series-b", chapter="12"
            ),
        )  # fmt: skip


def key_expiring(settings: Settings) -> None:
    with headless(settings) as h:
        added = h.admin.target_add_kavita(
            name="kavita", url=KAVITA_URL, username="shelfhold", password="p" * 12,
            accept_admin_risk=True,
        )  # fmt: skip
        with h.engine.begin() as connection:
            health = load_health(connection, added.id)
            health.key_notices.sent = frozenset({"J-14"})
            health.refresh_waiting()
            from shelfhold.export.application.target_state import save_health

            save_health(connection, added.id, health)
        ops = OpsServices(h.engine, clock=h.clock, bus=_bus(h), headless=h.admin)
        _listed_then_gone(
            ops, "key_expiring", lambda: ops.target_rotate_key(added.id, password="p" * 12)
        )
        assert len(h.kavita.calls) == 2  # a new key was asked of Kavita


def auth_expired(settings: Settings) -> None:
    with headless(settings) as h:
        h.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret="admin:old-secret")
        pause_follow(h.engine, failure_class="auth", state="auth_expired")
        ops = OpsServices(h.engine, clock=h.clock, bus=_bus(h), headless=h.admin)
        _listed_then_gone(ops, "auth_expired", lambda: ops.source_set_secret("admin:new-secret"))
        assert h.suwayomi.calls[-1]["password"] == "new-secret"


def auth_mode_unsupported(settings: Settings) -> None:
    with headless(settings) as h:
        h.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret="admin:s3cret")
        pause_follow(h.engine, failure_class="auth_mode", state="auth_mode_unsupported")
        h.suwayomi.mode = "basic_auth"  # the server was set back to a supported mode
        ops = OpsServices(h.engine, clock=h.clock, bus=_bus(h), headless=h.admin)
        _listed_then_gone(ops, "auth_mode_unsupported", ops.source_recheck)


def _bus(h: Headless):  # type: ignore[no-untyped-def]
    from shelfhold.events.bus import EventBus

    return EventBus(h.clock)


SCENARIOS: dict[str, Callable[..., None]] = {
    s.__name__: s
    for s in (
        missing, paused_foreign_writer, dead_letter, instance_changed, target_forbidden,
        key_expiring, not_indexed, auth_expired, adoption_refused, permission_denied,
        child_exposure, auth_mode_unsupported, coverage_unknown, language_unknown,
        restored_behind, tombstone_key_unknown, route_missing,
    )
}  # fmt: skip


# -- the parametrised test ----------------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(WaitingState, key=str), ids=str)
def test_every_waiting_state_has_exit_command(state: WaitingState, settings: Settings) -> None:
    routes = [r for r in EXIT_ROUTES if r.waiting_state == state.value]
    if state in LATER_STATES:
        # the exit arrives with the milestone where the state appears (EF-INT-023): until then it
        # is declared in the catalogue and has no scenario; M3 states have no route yet
        assert (state.value in EXIT_STATES_LATER) == (not routes)
        assert state not in M1A_STATES
        return
    assert state in M1A_STATES and routes, f"{state} has no exit command"
    assert state.value in SCENARIOS, f"{state} has no end-to-end scenario"
    for route in routes:
        assert route.command in ENDPOINTS, f"{route.command}: no API route declared"
        method, path = endpoint_of(route.command)
        assert path.startswith("/api/v1/"), route.command
        pending = CLI_PENDING.get(bare(route.command))
        if pending is None:
            assert _cli_has(route.command), f"`shelfhold {route.command}` is not a command"
        else:  # the CLI module belongs to a lot still in flight: the day it lands, remove the entry
            assert not _cli_has(route.command), f"{route.command} exists: drop it from CLI_PENDING"
    scenario = SCENARIOS[state.value]
    if "settings" in inspect.signature(scenario).parameters:
        scenario(settings)
    else:
        scenario()


@pytest.mark.parametrize("command", sorted(ENDPOINTS))
def test_every_exit_route_is_in_the_openapi(command: str) -> None:
    assert _route_documented(command), command


def test_the_enumeration_is_partitioned_into_m1a_and_later() -> None:
    assert M1A_STATES | LATER_STATES == set(WaitingState)
    assert not (M1A_STATES & LATER_STATES)
    assert {r.waiting_state for r in EXIT_ROUTES if r.waiting_state in {s.value for s in M1A_STATES}} == {
        s.value for s in M1A_STATES
    }


def test_language_unknown_has_exit_command(tmp_path: Path) -> None:
    """ENF-I18N-005: `edition set-lang` leaves `language_unknown`, and refuses a tag that would
    collide with another Edition of the same Work or a placeholder."""
    assert _cli_has("edition set-lang")
    with export_env() as env:
        series = env.new_series(lang="x-unknown")
        ops = make_ops(env)
        with pytest.raises(Exception, match="language"):
            ops.edition_set_lang(series.edition_id, "und")  # a wildcard is not a language
        assert edition_lang(env.engine, series.edition_id) == "x-unknown"
        # another Edition of the same Work already has that language: no silent merge
        with env.engine.begin() as connection:
            work = connection.execute(
                sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == series.edition_id)
            ).scalar_one()
        twin = new_ulid()
        with env.engine.begin() as connection:
            row = dict(
                connection.execute(
                    sa.select(catalog.edition).where(catalog.edition.c.id == series.edition_id)
                ).one()._mapping
            )
            connection.execute(sa.insert(catalog.edition).values({**row, "id": twin, "lang": "ja"}))
        with pytest.raises(Exception, match="language"):
            ops.edition_set_lang(series.edition_id, "ja")
        assert work
        assert ops.edition_set_lang(series.edition_id, "fr-CA") == "fr-CA"


def test_waiting_state_scenarios_cover_the_m1a_states() -> None:
    assert set(SCENARIOS) == {s.value for s in M1A_STATES}
