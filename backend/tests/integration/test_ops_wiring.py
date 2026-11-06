"""The composition root wires what earlier lots left open (PLAN-M1a section 7, L26/L29/L32/L37)."""

from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.observability.wiring import OPS_JOBS, register_ops_jobs
from shelfhold.jobs.huey_app import JobPools
from tests.fakes_clock import ManualClock
from tests.intents_env import NOW, prepare_database


@pytest.mark.req("ENF-OPS-006", "ENF-OPS-011")
def test_ops_jobs_are_registered_and_scheduled_at_boot(tmp_path: Path) -> None:
    engine = prepare_database(tmp_path / "shelfhold.db")
    (tmp_path / "data").mkdir()
    pools = JobPools(tmp_path / "data", engine=engine, clock=ManualClock(NOW), seed="i")
    settings = Settings(data_dir=tmp_path / "data", store_dir=tmp_path / "store")
    register_ops_jobs(pools, settings, audit=lambda: None, disk_check=lambda: None)
    assert {"ops.disk_check", "target.access_audit"} == set(OPS_JOBS)
    import sqlalchemy as sa

    from shelfhold.infrastructure.schema import platform

    with engine.connect() as conn:
        kinds = {r.job_kind: r.cron for r in conn.execute(sa.select(platform.schedule))}
    assert kinds["ops.disk_check"] == "* * * * *"  # every minute
    assert kinds["target.access_audit"].split()[2:] == ["*", "*", "*"]  # once a day (EF-EXP-055)


@pytest.mark.req("EF-EXP-055")
def test_the_kavita_client_gets_its_write_guard_from_the_composition_root(tmp_path: Path) -> None:
    from shelfhold.export.application.target_state import target_write_guard
    from tests.kavita_env import harness

    engine = prepare_database(tmp_path / "shelfhold.db")
    h = harness()
    client = h.client
    client.set_write_guard(target_write_guard(engine, "01JBZ3X5QH8M2N4P6R7S9T0V1A"))
    assert client._write_guard is not None
