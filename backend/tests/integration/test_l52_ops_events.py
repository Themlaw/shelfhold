"""L52: dead letters, operating events on the real bus, the start-up probe, the compose that shows
the read-only adoption mount. Everything goes through the production path: the pools, the runtime
boot, the bus of the application with its notification channels."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from shelfhold.integrations.apprise import operating_bus
from shelfhold.jobs.huey_app import JobPools
from shelfhold.runtime import InstanceRuntime
from tests.fakes_clock import ManualClock
from tests.l41_env import Instance, instance
from tests.l52_env import add_channel, events_of, name_channel, notified
from tests.notifications_env import NOW, notify_env

REPO = Path(__file__).resolve().parents[3]
SENTINEL = ".shelfhold-mount-id"


def boot_runtime(inst: Instance) -> InstanceRuntime:
    """The server's own start: the instance lock, then the open intents and the jobs."""
    runtime = InstanceRuntime(inst.settings)
    runtime.composition = inst.comp  # what `create_app` hands over: one bus, one lease identity
    runtime.acquire()
    runtime.boot()
    return runtime


def end(runtime: InstanceRuntime) -> None:
    runtime.shutdown(checkpoint_s=0.5)
    runtime.stop()


# -- ENF-OPS-011: a job that gave up is a dead letter, announced on the bus ------------------------


@pytest.mark.req("ENF-OPS-011", "EF-INT-023")
def test_retries_exhausted_sets_dead_letter_and_emits_job_dead_lettered(settings: Settings) -> None:
    with notify_env(settings) as env:
        env.add_channel("ops")
        clock = ManualClock(NOW)
        pools = JobPools(
            settings.data_dir, engine=env.engine, clock=clock, seed="l52",
            bus=operating_bus(clock), retry_backoff_s=(0.2, 0.2),
        )  # fmt: skip
        pools.register("boom", pool="io", handler=lambda job: 1 / 0)
        job_id = pools.submit("boom", dedupe_key="boom:1").job_id
        seen: list[tuple[str, int]] = []
        pools.start_workers()  # the real Huey consumers: RetryTask is Huey's own re-delivery
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                with env.engine.connect() as connection:
                    row = connection.execute(
                        sa.select(platform.job_view.c.status, platform.job_view.c.attempts)
                    ).one()
                if not seen or seen[-1] != (row.status, row.attempts):
                    seen.append((row.status, row.attempts))
                if row.status == "dead_letter":
                    break
                time.sleep(0.05)
        finally:
            pools.stop_workers()
        assert seen[-1] == ("dead_letter", 3)  # tried three times, then gave up
        assert ("queued", 1) in seen  # and was put back in between, not failed at the first
        with env.engine.connect() as connection:
            job = connection.execute(sa.select(platform.job_view)).one()
        assert job.error_code == "job.failed" and job.last_error == "ZeroDivisionError"
        assert events_of(env.engine, "job.dead_lettered") == [
            {"job_id": job_id, "job_kind": "boom", "reason": "retries_exhausted"}
        ]
        assert notified(env.engine, "job.dead_lettered") == ["ops"]  # the real channel
        with env.engine.connect() as connection:  # a dead letter is not an active job any more
            assert (
                connection.execute(
                    sa.select(sa.func.count()).where(
                        platform.job_view.c.status.in_(("queued", "running", "paused"))
                    )
                ).scalar_one()
                == 0
            )


# -- ENF-OBS-005: the operating events of the start-up reach the channels -------------------------


@pytest.mark.req("ENF-OBS-005", "ENF-OPS-011")
def test_reconcile_drift_and_restored_behind_create_notifications(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        add_channel(inst.comp)
        foreign = inst.library / "Kavita" / "someone-else.cbz"
        foreign.write_bytes(b"not what Shelfhold was writing")
        fsops, _loaded, _tombstones = inst.comp.read_context()
        IntentJournal(inst.engine, fsops, clock=inst.comp.clock).record(
            op="write", dst=str(foreign), expected_sha256="sha256:" + "0" * 64
        )  # an abrupt stop left it open, and the disk says something else
        runtime = boot_runtime(inst)
        end(runtime)
        drift = events_of(inst.engine, "reconcile.drift")
        assert len(drift) == 1 and drift[0]["kind"] == "unsettled_intent"
        assert notified(inst.engine, "reconcile.drift") == ["ops"]

        sentinel = next(
            p / SENTINEL for p in (inst.library, inst.library.parent) if (p / SENTINEL).exists()
        )
        document = json.loads(sentinel.read_text("utf-8"))
        document["restore_epoch"] = 1000  # the disk is ahead of the database (a restore)
        sentinel.write_text(json.dumps(document), "utf-8")
        runtime = boot_runtime(inst)
        end(runtime)
        assert len(events_of(inst.engine, "instance.restored_behind")) == 1
        assert notified(inst.engine, "instance.restored_behind") == ["ops"]


# -- EF-UX-002: the folder that cannot be written is said at the start, with the cause ------------


@pytest.mark.req("EF-UX-002", "ENF-OPS-011")
@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere: no 0500 folder refuses it")
def test_startup_probe_on_0500_root_reports_cause(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        add_channel(inst.comp)
        inst.library.chmod(0o500)  # read and traverse, no write: the classic wrong uid
        try:
            runtime = boot_runtime(inst)  # the start does not crash on it
            try:
                failed = [p for p in runtime.startup_probe if not p.ok]
            finally:
                inst.library.chmod(0o700)  # the shutdown needs to release what it took
                end(runtime)
        finally:
            inst.library.chmod(0o700)
        assert len(failed) == 1
        report = failed[0]
        assert report.folder == str(inst.library)
        assert report.cause == "permission_denied" and report.event == "target.permission_denied"
        owner = inst.library.stat()
        assert f'user: "{owner.st_uid}:{owner.st_gid}"' in report.yaml_hint  # the block to paste
        assert str(inst.library) in report.yaml_hint
        events = events_of(inst.engine, "target.permission_denied")
        assert len(events) == 1 and events[0]["reason"] == "forbidden"
        assert notified(inst.engine, "target.permission_denied") == ["ops"]


# -- EF-EXP-060: the compose shows the read-only adoption mount ----------------------------------


@pytest.mark.req("EF-EXP-060")
def test_compose_documents_adopt_ro_mount() -> None:
    text = (REPO / "compose.yaml").read_text("utf-8")
    mount = [line for line in text.splitlines() if "/upstream/suwayomi" in line]
    assert mount, "the reference compose does not show the adoption mount"
    assert any(line.rstrip().endswith(":ro") for line in mount)  # read-only, always
    assert "adopt_ro" in text  # named as the PathMap mode that goes with it
    lowered = text.lower()
    assert "remove" in lowered and "switchover" in lowered  # taken out once the switch is done
    assert "/upstream/suwayomi" not in "\n".join(  # but never active by default
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )


# -- ENF-OBS-005: the mirror reports on the operating bus, not on a bare one ---------------------


@pytest.mark.req("ENF-OBS-005", "EF-EXP-063")
def test_mirror_left_alone_is_notified_on_the_operating_bus() -> None:
    from tests.export_env import export_env
    from tests.integration.test_provenance_mirror_forged import mirror_file, not_json

    with export_env() as env:
        name_channel(env.engine, "ops")
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        not_json(mirror_file(env))  # unreadable as a whole: left alone and reported
        env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert env.pipeline.mirror.skipped
        assert set(notified(env.engine, "reconcile.drift")) == {"ops"}


# -- ENF-OPS-011: the keyring that cannot be read stops the start and says so --------------------


@pytest.mark.req("ENF-OPS-011", "ENF-SEC-005")
def test_unreadable_keyring_stops_the_start_and_publishes_secret_undecryptable(
    settings: Settings,
) -> None:
    from fastapi.testclient import TestClient

    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.infrastructure.migrate import migrate
    from shelfhold.infrastructure.secrets.diagnostics import boot_check, keyring_path
    from shelfhold.infrastructure.secrets.errors import KeyringError
    from shelfhold.main import create_app

    migrate(settings)
    boot_check(settings, announce=False)
    keyring_path(settings).write_bytes(b"this is not a keyring")
    name_channel(create_db_engine(settings.database_path), "ops")
    with pytest.raises(KeyringError), TestClient(create_app(settings)):
        pass
    engine = create_db_engine(settings.database_path)
    assert events_of(engine, "secret.undecryptable") == [{"reason": "keyring_corrupt"}]
    assert notified(engine, "secret.undecryptable") == ["ops"]
    engine.dispose()


WRITES = {
    "mkdir",
    "unlink",
    "remove",
    "rmdir",
    "rename",
    "replace",
    "utime",
    "symlink",
    "link",
    "truncate",
    "chmod",
}


def tree_of(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.req("EF-UX-002")
def test_startup_probe_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same start with and without the probe leaves the same tree in the root."""
    from shelfhold.config import load_settings
    from shelfhold.infrastructure.fsops.probe import ProbeReport

    listings = []
    for probed in (False, True):
        base = tmp_path / str(probed)
        own = load_settings(
            {"SHELFHOLD_DATA_DIR": str(base / "data"), "SHELFHOLD_STORE_DIR": str(base / "store")}
        ).values
        with instance(own, base) as inst, monkeypatch.context() as patch:
            inst.configure()
            if not probed:
                patch.setattr(
                    InstanceRuntime, "_probe_root",
                    staticmethod(lambda *_a: ProbeReport(True, "")),
                )  # fmt: skip
            end(boot_runtime(inst))
            listings.append(tree_of(inst.library))
    assert listings[1] == listings[0]


@pytest.mark.req("EF-UX-002", "EF-EXP-060")
def test_startup_probe_never_writes_to_an_adopt_ro_root(tmp_path: Path) -> None:
    """A read-only adoption root is registered, and the start does not touch it at all."""
    from shelfhold.config import load_settings
    from shelfhold.export.domain.path_map import PathMode
    from shelfhold.infrastructure.schema import platform as p
    from tests.fs_spy import FsSpy

    own = load_settings(
        {
            "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
            "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
        }
    ).values
    with instance(own, tmp_path) as inst:
        inst.configure()
        upstream = tmp_path / "upstream" / "suwayomi"
        upstream.mkdir(parents=True)
        (upstream / "keep.cbz").write_bytes(b"theirs")
        with inst.engine.begin() as connection:
            connection.execute(
                sa.insert(p.physical_root).values(
                    root_id=new_ulid(), path_entry=str(upstream), mode=PathMode.ADOPT_RO.value
                )
            )
        before = tree_of(upstream)
        with FsSpy([upstream]).watching() as spy:
            end(boot_runtime(inst))
        writes = [a for a in spy.accesses if a.split()[0] in WRITES]
        assert writes == [] and tree_of(upstream) == before
