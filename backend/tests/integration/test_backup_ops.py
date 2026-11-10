"""EF-INT-014, EF-INT-015, ENF-OPS-001, EF-INT-006 (M1b L23): the daily job, `status`, the events,
and `shelfhold backup run|verify|restore` as an operator types them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import load_settings
from shelfhold.events.bus import PAYLOAD_ENUMS
from shelfhold.events.operating import OPERATING_EVENTS
from shelfhold.infrastructure.schema import platform
from shelfhold.interfaces import cli
from shelfhold.interfaces.cli import status as status_command
from shelfhold.jobs.public import JobPools
from shelfhold.ops.backup import BACKUP_FAILURES, BACKUP_KIND, register_backup_job
from shelfhold.ops.backup import run as backup_run_module
from shelfhold.ops.backup.jobs import BACKUP_CRON
from tests.backup_env import MARKER, PASSPHRASE, backup_bench, write_passphrase

pytestmark = pytest.mark.req("EF-INT-014", "EF-INT-015", "ENF-OPS-001")


def run_job(pools: JobPools, engine: sa.Engine, name: str) -> str:
    job = pools.submit(BACKUP_KIND, dedupe_key=name)
    huey = pools.huey("maint")
    while (task := huey.dequeue()) is not None:
        huey.execute(task)
    with engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(platform.job_view.c.status).where(platform.job_view.c.id == job.job_id)
            ).scalar_one()
        )


def test_the_backup_job_is_daily_and_outside_the_window(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        pools = JobPools(bench.settings.data_dir, engine=bench.engine, clock=bench.clock, seed="b")
        register_backup_job(pools, bench.service)
        with bench.engine.connect() as connection:
            (row,) = connection.execute(sa.select(platform.schedule)).all()
        assert row.job_kind == BACKUP_KIND and row.cron == BACKUP_CRON
        assert row.window is None  # not a window job: it reads no store and no library disk
        _minute, hour, *rest = BACKUP_CRON.split()
        assert rest == ["*", "*", "*"]  # every day
        # 01:40 in the instance's zone, before the default window (03:00-07:00) opens
        assert int(hour) < 3
        assert run_job(pools, bench.engine, "first") == "succeeded"
        assert len(bench.names()) == 1


def test_the_job_fails_under_backup_failed_and_stays_quiet_when_nothing_is_configured(
    tmp_path: Path,
) -> None:
    with backup_bench(tmp_path, backup_passphrase_file=None) as bench:
        pools = JobPools(bench.settings.data_dir, engine=bench.engine, clock=bench.clock, seed="b")
        register_backup_job(pools, bench.service)
        assert (
            run_job(pools, bench.engine, "half") == "failed"
        )  # a backup dir without a passphrase: asked for
        assert bench.events("backup.failed") == [{"reason": "passphrase_missing"}]
    with backup_bench(tmp_path / "bare", backup_passphrase_file=None, backup_dir=None) as bench:
        pools = JobPools(bench.settings.data_dir, engine=bench.engine, clock=bench.clock, seed="b")
        register_backup_job(pools, bench.service)
        assert (
            run_job(pools, bench.engine, "bare") == "succeeded"
        )  # a fresh install: nothing to fail
        assert bench.events("backup.failed") == []


def test_the_job_gives_up_on_a_vacuum_that_does_not_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_run_module, "GATE_WAIT_S", 0.2)
    with backup_bench(tmp_path) as bench, bench.gate.exclusive() as ours:
        assert ours
        pools = JobPools(bench.settings.data_dir, engine=bench.engine, clock=bench.clock, seed="b")
        register_backup_job(pools, bench.service)
        assert run_job(pools, bench.engine, "busy") == "failed"
        assert bench.events("backup.failed") == [{"reason": "busy"}]
        assert not bench.names()


def test_the_events_and_the_error_codes_agree() -> None:
    assert {"backup.failed", "backup.stale"} <= OPERATING_EVENTS
    assert PAYLOAD_ENUMS["backup.failed"]["reason"] == BACKUP_FAILURES
    for reason in BACKUP_FAILURES:  # every cause has its sentence for the operator
        from shelfhold.i18n import t

        assert t(f"backup.failed_{reason}")


def test_status_carries_the_durable_backup_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with backup_bench(tmp_path) as bench:
        bench.service_for(
            bench.settings.model_copy(
                update={"backup_passphrase_file": write_passphrase(bench.settings.data_dir / "pw")}
            )
        ).run(accept_colocated=True)
        env = {
            "SHELFHOLD_DATA_DIR": str(bench.settings.data_dir),
            "SHELFHOLD_STORE_DIR": str(bench.settings.store_dir),
            "SHELFHOLD_BACKUP_DIR": str(bench.backups),
            "SHELFHOLD_BACKUP_PASSPHRASE_FILE": str(bench.passphrase),
        }
        resolved = load_settings(env)
        codes = {w["code"] for w in status_command.warnings(resolved)}
        assert "backup_passphrase_colocated" in codes
        unconfigured = load_settings(
            {k: v for k, v in env.items() if not k.startswith("SHELFHOLD_BACKUP")}
        )
        assert "backup_not_configured" in {w["code"] for w in status_command.warnings(unconfigured)}


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    with backup_bench(tmp_path) as bench:
        values = {
            "SHELFHOLD_DATA_DIR": str(bench.settings.data_dir),
            "SHELFHOLD_STORE_DIR": str(bench.settings.store_dir),
            "SHELFHOLD_BACKUP_DIR": str(bench.backups),
            "SHELFHOLD_BACKUP_PASSPHRASE_FILE": str(bench.passphrase),
        }
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        yield bench


def test_cli_backup_run_verify_restore(
    cli_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    bench = cli_env
    assert cli.main(["backup", "run"]) == 0
    assert "Sauvegarde faite" in capsys.readouterr().out
    (name,) = bench.names()
    assert cli.main(["backup", "verify"]) == 0
    assert "Sauvegarde vérifiée" in capsys.readouterr().out
    assert cli.main(["backup", "verify", "--file", str(bench.backups / name)]) == 0
    capsys.readouterr()

    target = tmp_path / "restored"
    archive = str(bench.backups / name)
    assert cli.main(["backup", "restore", "--from", archive, "--to", str(target)]) == 0
    out = capsys.readouterr().out
    assert "faite sous" in out and "reconcile --adopt-after-restore" in out
    assert (target / "shelfhold.db").is_file() and (target / "keyring").is_file()
    # a second restore onto the same folder, and one onto the live /data, are refused
    assert cli.main(["backup", "restore", "--from", archive, "--to", str(target)]) == 1
    assert "pas un dossier vide" in capsys.readouterr().err
    live = str(bench.settings.data_dir)
    assert cli.main(["backup", "restore", "--from", archive, "--to", live]) == 1
    assert "autre chemin" in capsys.readouterr().err
    # a damaged archive: one sentence, exit 1, and the destination is not created
    damaged = bench.backups / name
    damaged.write_bytes(damaged.read_bytes()[:-5])
    assert cli.main(["backup", "verify"]) == 1
    first = capsys.readouterr().err
    assert cli.main(["backup", "restore", "--from", archive, "--to", str(tmp_path / "x")]) == 1
    assert capsys.readouterr().err == first
    assert not (tmp_path / "x").exists()
    assert MARKER not in first and PASSPHRASE not in first


def test_cli_backup_run_without_passphrase_says_so_and_writes_nothing(
    cli_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    bench = cli_env
    monkeypatch.delenv("SHELFHOLD_BACKUP_PASSPHRASE_FILE")
    assert cli.main(["backup", "run"]) == 1
    assert "phrase de passe absente" in capsys.readouterr().err
    assert not bench.backups.exists()
    with bench.engine.connect() as connection:
        row = connection.execute(sa.select(platform.backup_run)).one()
    assert (row.state, row.cause) == ("failed", "passphrase_missing")
    assert json.dumps(bench.events("backup.failed")) == '[{"reason": "passphrase_missing"}]'
