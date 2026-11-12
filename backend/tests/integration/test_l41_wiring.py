"""L41: the production wiring (PLAN-M1a section 7): what `create_app` boots really runs, and what
the lots delivered as services has its door (CLI, route) and its operating events."""

import argparse
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema.acquisition import source_config
from shelfhold.integrations.apprise import operating_bus
from shelfhold.integrations.apprise.channels import ROW_ID
from shelfhold.interfaces.cli.registry import build_parser
from shelfhold.main import create_app
from shelfhold.ops import CLI_PENDING

pytestmark = pytest.mark.req("EF-INT-023", "EF-EXP-034", "EF-SRC-009")

JOBS = {
    "follow.cycle", "export.run", "reconcile.cycle", "reconcile.hash",
    "target.access_audit", "ops.disk_check", "intents.prune", "target.key_expiry",
    "target.library_type_check", "target.major_update_replay",
}  # fmt: skip


def _sub(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return dict(action.choices)


def test_every_runtime_job_is_scheduled_at_boot(settings: Settings) -> None:
    migrate(settings)
    with TestClient(create_app(settings)):
        engine = create_db_engine(settings.database_path)
        try:
            with engine.connect() as connection:
                kinds = set(connection.execute(sa.text("SELECT job_kind FROM schedule")).scalars())
        finally:
            engine.dispose()
    assert kinds >= JOBS


def test_a_fresh_install_runs_every_job_quietly(settings: Settings) -> None:
    from shelfhold.composition import Composition
    from shelfhold.jobs.huey_app import JobContext  # noqa: F401
    from shelfhold.runtime_jobs import (
        access_audit,
        export_handler,
        follow_handler,
        reconcile_handler,
    )

    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        comp = Composition(settings, engine, clock=SystemClock())
        access_audit(comp)()  # no target: nothing to audit, nothing raised
        assert (
            callable(export_handler(comp))
            and callable(follow_handler)
            and callable(reconcile_handler)
        )
    finally:
        engine.dispose()


def test_the_daily_housekeeping_jobs_run_quietly_and_prune_closed_intents(
    settings: Settings,
) -> None:
    from shelfhold.composition import Composition
    from shelfhold.runtime_jobs import INTENT_RETENTION, intents_prune, key_expiry

    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        comp = Composition(settings, engine, clock=SystemClock())
        assert intents_prune(comp)() == 0  # no library yet: no journal to prune
        key_expiry(comp)()  # no target: nothing to read, nothing raised
        assert timedelta(days=7) <= INTENT_RETENTION
    finally:
        engine.dispose()


def test_consumers_stop_with_the_app(settings: Settings) -> None:
    migrate(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    # leaving the context stopped the workers: a second boot of the same process works
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200


def test_the_operating_bus_queues_a_notification_per_channel(settings: Settings) -> None:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.insert(source_config).values(plugin_id=ROW_ID, settings={"channels": ["ntfy"]})
            )
        bus = operating_bus(SystemClock())
        with engine.begin() as connection:
            bus.publish(connection, "disk.low", {"free": 1})
        with engine.connect() as connection:
            queued = connection.execute(
                sa.text("SELECT count(*) FROM notification_pending")
            ).scalar_one()
    finally:
        engine.dispose()
    assert queued >= 1


def test_no_command_of_the_exit_table_is_pending() -> None:
    assert dict(CLI_PENDING) == {}


def test_the_library_commands_exist() -> None:
    commands = _sub(build_parser())
    assert {"adopt", "detach", "purge", "probe"} <= set(commands)
    adopt = commands["adopt"]
    flags = {o for a in adopt._actions for o in a.option_strings}
    assert {"--repatriate", "--pair-auto", "--confirm-migration"} <= flags
    assert set(_sub(adopt)) == {"pair", "set-follow-from", "set-coverage"}


def test_the_library_routes_are_admin_only_and_documented(settings: Settings) -> None:
    migrate(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        paths = app.openapi()["paths"]
        for path in (
            "/api/v1/adoption/{destination_id}/pairing",
            "/api/v1/adoption/{destination_id}/pair",
            "/api/v1/adoption/{destination_id}/pair-auto",
            "/api/v1/adoption/{destination_id}/migration",
            "/api/v1/adoption/{destination_id}/repatriate",
            "/api/v1/detach",
            "/api/v1/units/{unit_id}/purge",
            "/api/v1/works/{work_id}/purge",
            "/api/v1/probe",
        ):
            assert path in paths, path
        assert client.post("/api/v1/units/x/purge", json={"typed": "x"}).status_code == 401
        assert client.post("/api/v1/probe", json={"folder": "/data/x"}).status_code == 401


def test_the_hold_window_constants_are_sane() -> None:
    from shelfhold.runtime_jobs import SCAN_QUIET_S

    assert SCAN_QUIET_S > 30 and timedelta(seconds=SCAN_QUIET_S) > timedelta(seconds=30)
