"""Review of PR #46 (L27): every proof of concept of the adversarial review, as a regression test.

Sqlite atomicity of `begin_nested`, the `write` intent, the cross-shelf collision, overlaps found
through links and inodes, one destination per shelf, strong `If-Match`, admin-only plans.
"""

import inspect
import os
import signal
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi import Request

import shelfhold.export.application.pipeline as pm
from shelfhold.config import Settings
from shelfhold.export.application.export_items import SeriesCollisionError, ensure_export_series
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.manifest_write import adopt_written_hook
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.snapshot import load_context, version_of
from shelfhold.export.domain.bindings import (
    BindingFacts,
    ShelfAlreadyBoundError,
    TreeOverlapError,
    check_binding,
)
from shelfhold.export.domain.planner import SeriesRef
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.interfaces.http_api.plans import build_plans_router, require_admin
from shelfhold.interfaces.http_api.problems import ProblemError
from tests.auth_env import auth_env
from tests.export_env import ExportEnv, add_second_shelf, export_env
from tests.integration.test_export_routes import FakePipeline, FakePreview


def count(env: ExportEnv, table: str) -> int:
    with env.engine.connect() as c:
        return int(c.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608


def shelf_of(env: ExportEnv, destination_id: str) -> str:
    with env.engine.connect() as c:
        return str(
            c.execute(
                sa.select(export.shelf_destination.c.shelf_id).where(
                    export.shelf_destination.c.id == destination_id
                )
            ).scalar_one()
        )


def resume(env: ExportEnv) -> object:
    ResourceLocks(env.engine, clock=env.clock).release_all()  # what the boot does first
    journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)
    hook = adopt_written_hook(env.lib.fsops, env.lib.locate, env.clock)
    return journal.resolve_open(on_done=hook)


def run_killed(env: ExportEnv, patch: str, attribute: str) -> None:
    """A real child process that dies by SIGKILL right when `attribute` of `patch` is reached."""
    env.engine.dispose()
    pid = os.fork()
    if pid == 0:  # child
        try:
            target = IntentJournal if patch == "journal" else pm
            setattr(target, attribute, lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL))
            env.pipeline.run(env.destination_id, job_id=new_ulid())
        finally:
            os._exit(3)
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL


# -- blocker 1: SAVEPOINT atomicity under pysqlite ---------------------------------------------


@pytest.mark.req("ENF-OPS-012")
def test_an_error_after_a_successful_begin_nested_leaves_nothing(tmp_path: Path) -> None:
    engine = create_db_engine(tmp_path / "t.db")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE t (x INTEGER)")
    with pytest.raises(RuntimeError), engine.connect() as c, c.begin():
        with c.begin_nested():
            c.exec_driver_sql("INSERT INTO t VALUES (1)")
        raise RuntimeError("after the savepoint was released")
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM t").scalar_one() == 0
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000


@pytest.mark.req("ENF-OPS-012")
def test_a_series_reserved_in_a_transaction_that_fails_is_not_kept() -> None:
    with export_env() as env:
        env.new_series("Foo")
        with env.engine.connect() as c:
            library = load_context(c, env.destination_id).remote_library_id
            edition = c.execute(sa.text("SELECT id FROM edition")).scalars().first()
        assert library is not None and edition is not None
        with pytest.raises(RuntimeError), env.engine.begin() as c:
            ensure_export_series(
                c, shelf_destination_id=env.destination_id, remote_library_id=library,
                ref=SeriesRef(edition, "Foo", "foo"), now=env.clock.now(),
            )  # fmt: skip
            raise RuntimeError("the next statement failed")
        assert count(env, "export_series") == 0 and count(env, "edition_export_series") == 0


# -- blocker 2: the write goes through the intent journal ---------------------------------------


@pytest.mark.req("EF-EXP-041")
def test_kill9_between_placement_and_manifest_is_adopted_at_resume() -> None:
    with export_env() as env:
        env.add_chapter(env.new_series(), "2")
        run_killed(env, "module", "complete_write")
        assert len(env.files()) == 1  # placed
        assert [r.state for r in env.rows(export.export_record)] == ["planned"]  # reserved only
        with env.engine.connect() as c:
            assert c.execute(sa.text("SELECT op, state FROM fs_intent")).all() == [
                ("write", "applying")
            ]
        report = resume(env)
        assert len(report.completed) == 1 and report.still_open == []  # type: ignore[attr-defined]
        record = env.rows(export.export_record)
        assert [r.state for r in record] == ["written"] and record[0].size is not None
        with env.engine.connect() as c:
            assert c.execute(sa.text("SELECT state FROM fs_intent")).scalars().all() == ["done"]
        again = env.pipeline.preview(env.destination_id)
        assert again.ops == () and again.blocked == ()  # nothing is orphaned or blocked
        assert len(env.files()) == 1


@pytest.mark.req("EF-EXP-041")
def test_kill9_before_the_placement_closes_the_intent_and_the_next_run_writes() -> None:
    with export_env() as env:
        env.add_chapter(env.new_series(), "2")
        run_killed(env, "journal", "mark_applying")
        assert env.files() == []
        report = resume(env)
        assert len(report.abandoned) == 1 and report.still_open == []  # type: ignore[attr-defined]
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert len(env.files()) == 1
        assert [r.state for r in env.rows(export.export_record)] == ["written"]


@pytest.mark.req("EF-EXP-041")
def test_a_foreign_file_at_the_target_is_a_drift_and_stays_untouched() -> None:
    with export_env() as env:
        env.add_chapter(env.new_series(), "2")
        run_killed(env, "journal", "mark_applying")
        with env.engine.connect() as c:
            dst = c.execute(sa.text("SELECT dst_path FROM fs_intent")).scalar_one()
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"somebody else's file")
        report = resume(env)
        assert len(report.drifted) == 1  # type: ignore[attr-defined]
        assert Path(dst).read_bytes() == b"somebody else's file"
        with env.engine.connect() as c:
            assert c.execute(sa.text("SELECT state FROM fs_intent")).scalars().all() == ["pending"]
        assert [r.state for r in env.rows(export.export_record)] == ["planned"]


@pytest.mark.req("EF-EXP-041")
def test_a_failure_after_placement_keeps_the_intent_and_the_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with export_env() as env:
        env.add_chapter(env.new_series(), "2")

        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr(pm, "complete_write", boom)
        with pytest.raises(RuntimeError):
            env.pipeline.run(env.destination_id, job_id=new_ulid())
        monkeypatch.undo()
        assert len(env.files()) == 1 and count(env, "export_series") == 1
        assert env.pipeline.preview(env.destination_id).blocked == ()  # covered, not a collision
        resume(env)
        assert [r.state for r in env.rows(export.export_record)] == ["written"]


# -- blocker 3: two shelves, one remote library ------------------------------------------------


@pytest.mark.req("EF-EXP-062")
def test_a_series_taken_by_another_shelf_makes_the_plan_stale_and_places_nothing() -> None:
    with export_env() as env:
        _, dest2 = add_second_shelf(env)
        a = env.new_series("Foo")
        b = env.new_series("Foo", shelf_id=shelf_of(env, dest2))
        env.add_chapter(a, "1")
        env.add_chapter(b, "1")
        pa, pb = env.pipeline.preview(env.destination_id), env.pipeline.preview(dest2)
        assert pa.blocked == () and pb.blocked == ()
        assert len(pa.base_version) == 32 and int(pa.base_version, 16) >= 0  # 128 bits, hex
        with env.engine.connect() as c:
            before = version_of(c, load_context(c, dest2))
        env.pipeline.apply(pa.plan_id, if_match=pa.base_version, job_id=new_ulid())
        with env.engine.connect() as c:
            assert version_of(c, load_context(c, dest2)) != before  # the series moved it
        with pytest.raises(StalePlanError):
            env.pipeline.apply(pb.plan_id, if_match=pb.base_version, job_id=new_ulid())
        assert env.lib.files_under(env.lib.manga / "Kavita2") == []
        assert len(env.rows(export.export_record)) == 1
        fresh = env.pipeline.preview(dest2)
        assert [x.code for x in fresh.blocked] == ["export.series_name_collision"]


@pytest.mark.req("EF-EXP-062")
def test_the_reservation_stops_a_racing_write_before_any_file_is_placed() -> None:
    with export_env() as env:
        _, dest2 = add_second_shelf(env)
        a = env.new_series("Foo")
        b = env.new_series("Foo", shelf_id=shelf_of(env, dest2))
        env.add_chapter(a, "1")
        env.add_chapter(b, "1")
        pb = env.pipeline.preview(dest2)
        env.pipeline.run(env.destination_id, job_id=new_ulid())  # the other shelf wins the name
        with env.engine.connect() as c:
            ctx = load_context(c, dest2)
        planned = pm.decode_op(pb.ops[0].__dict__ if False else pm.encode_op(pb.ops[0]))
        with pytest.raises(SeriesCollisionError):  # past the claim: the unique key still holds
            env.pipeline._execute(ctx, planned, pb.plan_id, new_ulid())
        assert env.lib.files_under(env.lib.manga / "Kavita2") == []
        assert count(env, "fs_intent") == 2  # only the first shelf's: its file and its mirror, done


# -- minors ------------------------------------------------------------------------------------


@pytest.mark.req("EF-EXP-045")
def test_parse_verified_has_no_default() -> None:
    parameter = inspect.signature(ExportPipeline).parameters["parse_verified"]
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.req("EF-EXP-058")
def test_overlap_is_seen_through_a_link_and_a_second_destination_is_refused(
    tmp_path: Path,
) -> None:
    with export_env() as env:
        real = env.lib.manga / "Kavita"
        (tmp_path / "alias").symlink_to(env.lib.manga)
        shelves = ShelfService(env.engine)

        def target(path: str) -> str:
            tid = new_ulid()
            with env.engine.begin() as c:
                c.execute(
                    sa.insert(export.export_target).values(
                        id=tid, kind="kavita", name=tid,
                        path_map=[{"local": path, "remote": "/m", "mode": "rw"}],
                    )
                )  # fmt: skip
            return tid

        def shelf() -> str:
            return shelves.create_shelf(name=new_ulid(), phrase="p")

        for transport in ("tree", "dropbox"):  # every writing transport, not only trees
            with pytest.raises(TreeOverlapError):
                shelves.bind_destination(
                    shelf_id=shelf(), target_id=target(f"{tmp_path}/alias/Kavita"),
                    transport=transport, plugin_egress="foyer",
                )  # fmt: skip
        assert real.exists()
        with pytest.raises(ShelfAlreadyBoundError):  # EF-EXP-002: one destination per shelf
            shelves.bind_destination(
                shelf_id=env.shelf_id, target_id=target(str(env.lib.manga / "Other")),
                transport="tree", plugin_egress="foyer",
            )  # fmt: skip
        ok = shelves.bind_destination(
            shelf_id=shelf(), target_id=target(str(env.lib.manga / "Other")),
            transport="tree", plugin_egress="foyer",
        )  # fmt: skip
        assert ok


@pytest.mark.req("EF-EXP-058")
def test_same_device_and_inode_is_the_same_folder_whatever_its_name() -> None:
    mine = BindingFacts("d1", "s1", "t1", "tree", "foyer", "/a/Kavita", (64769, 1234))
    other = BindingFacts("d2", "s2", "t2", "dropbox", "foyer", "/MNT/elsewhere", (64769, 1234))
    with pytest.raises(TreeOverlapError):
        check_binding(mine, [other], retention_mode="keep")
    apart = BindingFacts("d2", "s2", "t2", "dropbox", "foyer", "/MNT/elsewhere", (64769, 9))
    check_binding(mine, [apart], retention_mode="keep")


@pytest.mark.req("EF-EXP-042")
def test_if_match_must_be_one_strong_quoted_validator(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    pipeline = FakePipeline()
    with auth_env(monkeypatch, settings, routers=[build_plans_router(lambda: pipeline)]) as api:
        assert api.login().status_code == 200
        url = f"/api/v1/plans/{FakePreview.plan_id}/apply"
        for refused in ('W/"4242"', "4242", "*", '"4242", "4243"', '""'):
            answer = api.post(url, headers={"If-Match": refused})
            assert (answer.status_code, answer.json()["code"]) == (412, "precondition_failed")
        assert pipeline.applied == []
        assert api.post(url, headers={"If-Match": '"4242"'}).status_code == 200


@pytest.mark.req("EF-EXP-008")
def test_plans_are_for_admins() -> None:
    def request(principal: Principal) -> Request:
        scope = {"type": "http", "method": "POST", "headers": [], "state": {"principal": principal}}
        return Request(scope)

    def who(role: str, credential: Credential, scopes: tuple[str, ...] = ()) -> Principal:
        return Principal("u", "u", role, credential, session_id="s", key_id="k", scopes=scopes)

    assert require_admin(request(who("admin", Credential.SESSION)))
    assert require_admin(request(who("member", Credential.API_KEY, ("admin",))))
    for refused in (
        who("member", Credential.SESSION),
        who("child", Credential.SESSION),
        who("admin", Credential.API_KEY, ("request",)),
        who("admin", Credential.DEVICE_TOKEN),
    ):
        with pytest.raises(ProblemError) as raised:
            require_admin(request(refused))
        assert str(raised.value) == "forbidden"
