"""Review of PR #66: the six minor findings, one regression test each (`fix: ... (review)`)."""

import asyncio
import os
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.export.application.pipeline import ApplyReport
from shelfhold.export.application.root_bootstrap import (
    RootPolicy,
    RootRefusedError,
    register_roots,
)
from shelfhold.export.domain.path_map import PathMode
from shelfhold.export.targets.kavita.errors import TargetUnreachable
from shelfhold.headless import InvalidConfigValue
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops.errors import RootBlockedError
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import platform
from tests.auth_env import auth_env, run_cli
from tests.headless_env import KAVITA_URL, headless

pytestmark = pytest.mark.req("EF-EXP-059", "EF-INT-024", "EF-EXP-034", "EF-BIB-052", "EF-USR-024")
PASSWORD = "synthetic"  # noqa: S105


def _policy(settings: Settings, library):  # type: ignore[no-untyped-def]
    return RootPolicy(library, settings.data_dir, settings.store_dir)


def test_m1_roots_stay_under_the_library_and_apart_from_data_and_store(
    settings: Settings, tmp_path
) -> None:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    library = tmp_path / "library"
    (library / "manga").mkdir(parents=True)
    foreign = tmp_path / "other-service"
    foreign.mkdir()
    policy = _policy(settings, library)
    for bad in (foreign, settings.data_dir, library.parent):
        with pytest.raises(RootRefusedError):
            register_roots(engine, [(str(bad), PathMode.RW)], policy=policy)
        assert not (bad / ".shelfhold-mount-id").exists()
    register_roots(engine, [(str(library / "manga"), PathMode.RW)], policy=policy)
    with pytest.raises(RootRefusedError):  # the parent over a registered child
        register_roots(engine, [(str(library), PathMode.RW)], policy=policy)
    with pytest.raises(RootRefusedError):  # two overlapping entries of one path map
        register_roots(
            engine,
            [(str(library / "a"), PathMode.RW), (str(library / "a" / "b"), PathMode.RW)],
            policy=policy,
        )
    with engine.connect() as c:
        roots = c.execute(sa.select(platform.physical_root.c.path_entry)).scalars().all()
    assert roots == [str(library / "manga")]
    engine.dispose()


def test_m1_nothing_is_stamped_while_the_instance_is_frozen(settings: Settings, tmp_path) -> None:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    library = tmp_path / "library"
    (library / "manga").mkdir(parents=True)
    register_roots(engine, [], policy=_policy(settings, library))  # creates the instance row
    with engine.begin() as c:
        c.execute(sa.update(platform.instance).values(restore_generation=1))
    assert register_roots(engine, [(str(library / "manga"), PathMode.RW)]) == []
    assert not (library / "manga" / ".shelfhold-mount-id").exists()
    engine.dispose()


def test_m1_a_path_map_outside_the_library_is_refused_and_not_stored(
    settings: Settings, tmp_path
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with headless(settings, roots=_policy(settings, library)) as env:
        with pytest.raises(InvalidConfigValue):
            env.admin.target_add_kavita(
                name="k", url=KAVITA_URL, username="u", password=PASSWORD, accept_admin_risk=True,
                path_map=[{"local": str(outside), "remote": "/m", "mode": "rw"}],
            )  # fmt: skip
        assert env.count("export_target") == 0 and not (outside / ".shelfhold-mount-id").exists()
        (library / "manga").mkdir()
        env.admin.target_add_kavita(
            name="k", url=KAVITA_URL, username="u", password=PASSWORD, accept_admin_risk=True,
            path_map=[{"local": str(library / "manga"), "remote": "/m", "mode": "rw"}],
        )  # fmt: skip
        with pytest.raises(InvalidConfigValue):
            env.admin.set("path_map.k", f'[{{"local": "{outside}", "remote": "/m"}}]')
        assert env.admin.get("path_map.k").value[0]["local"] == str(library / "manga")


def test_m2_an_unwritable_root_does_not_stop_the_others(settings: Settings, tmp_path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    library = tmp_path / "library"
    bad, good = library / "a-ro", library / "b-manga"
    bad.mkdir(parents=True)
    good.mkdir()
    bad.chmod(0o555)
    try:
        created = register_roots(
            engine, [(str(bad), PathMode.RW), (str(good), PathMode.RW)],
            policy=_policy(settings, library),
        )  # fmt: skip
    finally:
        bad.chmod(0o755)
    assert len(created) == 1 and (good / ".shelfhold-mount-id").exists()
    engine.dispose()


def test_m3_a_destination_lost_after_the_writing_does_not_escape(monkeypatch) -> None:
    class Client:
        async def flush_scans(self, allowed: object = None) -> None:
            raise TargetUnreachable("Library/scan-folder: ConnectError")

    class Pipeline:
        def bind_parse(self, *_: object, **__: object) -> None: ...
        def run(self, *_: object, **__: object) -> ApplyReport:
            return ApplyReport(None, ())

    class Disks:
        def get(self) -> tuple[object, object]:
            return Pipeline(), object()

        def fsops(self) -> object:
            return object()

    async def fetch(*_: object, **__: object) -> object:
        return object()

    monkeypatch.setattr(rj, "SCAN_QUIET_S", 0)
    monkeypatch.setattr(rj.BulkParse, "fetch", staticmethod(fetch))

    async def no_rename(*_: object, **__: object) -> int:
        return 0

    async def no_probe(*_: object) -> None: ...

    monkeypatch.setattr(rj, "_probe_version", no_probe)  # no server nor database behind this stub
    monkeypatch.setattr(rj, "_names_may_refresh", lambda *_: False)
    monkeypatch.setattr(rj, "refresh_names", no_rename)  # no database behind this stub comp
    monkeypatch.setattr(rj, "wanted_names", lambda *_, **__: (["x"], None))
    scans = rj.ScanRelay()
    scans.asked = ["/manga/x"]
    context = SimpleNamespace(stop_requested=False, job_id="j")

    class Poller:
        def __init__(self, *_: object, **__: object) -> None: ...
        async def poll(self, *_: object) -> None:
            raise TargetUnreachable("poll")

    monkeypatch.setattr(rj, "ArrivalPoller", Poller)
    monkeypatch.setattr(rj, "_scan_guard", lambda *_a: lambda _folder: True)  # no database here

    async def no_listing(*_: object) -> int:
        return 0

    monkeypatch.setattr(rj, "refresh_foreign_names", no_listing)
    monkeypatch.setattr(rj, "BusTargetEvents", lambda *_: None)
    comp = SimpleNamespace(
        engine=None,
        clock=None,
        bus=None,
        settings=SimpleNamespace(naming_segment_policy_confirmed=False),
    )
    asyncio.run(
        rj._export_one(comp, Disks(), Client(), rj.Destination("d", "t"), scans, context)  # type: ignore[arg-type]
    )  # returns: the loop over the destinations goes on


def test_m4_a_blocked_root_and_a_frozen_instance_are_coded_refusals() -> None:
    from shelfhold.export.application.restore import RestoredBehindError
    from shelfhold.export.domain.physical_roots import RootState
    from shelfhold.ops.library import refusal_of

    frozen = refusal_of(RootBlockedError("/m", RootState.RESTORED_BEHIND))
    assert frozen is not None and frozen.code == "instance.restored_behind"
    missing = refusal_of(RootBlockedError("/m", RootState.MISSING))
    assert missing is not None and missing.code == "root.blocked"
    behind = refusal_of(RestoredBehindError("run `shelfhold reconcile --adopt-after-restore`"))
    assert behind is not None and "adopt-after-restore" in str(behind)


def test_m5_detach_without_restore_upstream_is_a_422(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    import re

    with auth_env(monkeypatch, settings) as api:
        assert api.login().status_code == 200
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "admin", "keys", "create") == 0
        key = re.search(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}", capsys.readouterr().out)
        assert key is not None
        sent = api.client.post(
            "/api/v1/detach", json={"restore_upstream": False},
            headers={"Authorization": f"Bearer {key.group(0)}"},
        )  # fmt: skip
        assert sent.status_code == 422


def test_m6_the_purge_takes_its_time_from_the_clock_of_the_composition() -> None:
    import inspect

    from shelfhold.ops.library import LibraryOps

    assert "now" not in inspect.signature(LibraryOps.purge).parameters


def test_a_periodic_job_can_be_asked_to_run_now(settings: Settings, tmp_path) -> None:
    from shelfhold.ops.errors import UnknownSubjectError
    from tests.l41_env import instance

    with instance(settings, tmp_path) as inst:
        from shelfhold.ops.wiring import open_ops

        ops = open_ops(settings, engine=inst.engine, composition=inst.comp)
        with pytest.raises(UnknownSubjectError):
            ops.jobs_run_now("follow.cycle")  # nothing scheduled yet on this fresh database
        with inst.engine.begin() as c:
            c.execute(
                sa.insert(platform.schedule).values(
                    id="01M6Q2VYM102EK250JTAPVGMV7",
                    job_kind="follow.cycle",
                    scope_key="",
                    cron="*/15 * * * *",
                    jitter_s=0,
                    enabled=True,
                )
            )
        ops.jobs_run_now("follow.cycle")
        with inst.engine.connect() as c:
            due = c.execute(sa.select(platform.schedule.c.next_due_at)).scalar_one()
        assert due is not None


def test_shelf_create_makes_the_folder_of_the_destination(settings: Settings, tmp_path) -> None:
    """The permission probe of the export and the Kavita library both need the folder to exist."""
    from shelfhold.headless.wiring import _ShelfLinker
    from tests.l41_env import instance

    class NoKavita(_ShelfLinker):
        def link(self, target_id: str, destination_id: str, *, library_name: str) -> str:
            return "1"

    with instance(settings, tmp_path) as probe:
        linker = NoKavita(settings, probe.engine)
    with instance(settings, tmp_path / "again", linker=linker) as inst:
        (inst.library / "Kavita").rmdir()
        inst.configure()
        assert (inst.library / "Kavita").is_dir()
