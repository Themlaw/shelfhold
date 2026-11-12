"""Review PoCs of PR #201 (M3 L13): maintenance mode and queue control, attacked.

Each test states what the lot promises and plays the case where it does not hold. Red on the
branch as reviewed (a701fa3f); a fix turns them green without changing their assertions.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import identity, platform
from shelfhold.jobs.control import PAUSE_CODE
from shelfhold.jobs.errors import JobFailedError
from shelfhold.jobs.huey_app import JobContext
from shelfhold.ops import OpsServices
from shelfhold.ops.errors import CodedRefusal
from tests.auth_env import auth_env, run_cli
from tests.integration.test_maintenance_mode import _Ops, set_mode
from tests.integration.test_plugin_cli import KEY, cli  # noqa: F401 - the fixture
from tests.maintenance_env import Env, env

pytestmark = pytest.mark.req("EF-INT-018", "EF-INT-017")

SAFEGUARD = "library.safeguard"
ACCESS_AUDIT = "target.access_audit"  # EF-EXP-055: a child account that sees an adult library


def watch(e: Env, ran: list[str], kind: str, pool: str = "io") -> None:
    e.pools.register(kind, pool=pool, handler=lambda c: ran.append(c.dedupe_key))


# -- the mode -----------------------------------------------------------------------------------


def test_review_poc_entry_between_the_mode_check_and_the_claim_lets_a_job_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_execute` reads the flag, then claims the row in another transaction. An entry that lands
    in between reports `running_jobs == 0` (the admin believes the instance is still) and the job
    starts all the same. Either the job does not start, or the entry counts it."""
    with env(tmp_path) as e:
        ran: list[str] = []
        watch(e, ran, "acquire.run")
        e.pools.submit("acquire.run", dedupe_key="x")
        real_claim = e.pools.ledger.claim_running
        reported: list[int] = []

        def racing(job_id: str) -> bool:
            reported.append(e.ops.enter(principal=e.principal("cli"))["running_jobs"])
            return real_claim(job_id)

        monkeypatch.setattr(e.pools.ledger, "claim_running", racing)
        e.run()
        assert reported == [0]
        assert ran == [], "a job started after an entry that reported nothing running"


def test_review_poc_logout_is_refused_while_the_mode_is_on(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Sign-in stays open, sign-out does not: on a shared screen, nobody can close a session for
    as long as the mode lasts (the guard lets only the three anonymous paths and the exit in)."""
    with auth_env(monkeypatch, settings, ops_services=_Ops()) as live:
        live.login()
        set_mode(live, True)
        out = live.post("/api/v1/auth/logout")
        assert out.status_code == 204, out.status_code


@pytest.mark.parametrize("method", ["identity.sessions_revoke", "identity.keys_revoke"])
def test_review_poc_the_socket_refuses_the_emergency_revocations_while_the_mode_is_on(
    tmp_path: Path, method: str
) -> None:
    """`admin sessions revoke` and `admin keys revoke` are the emergency exits (`_revocation`,
    lockless): while the mode is on they are refused like any write, so a stolen session or key
    keeps reading until the admin leaves the mode, which reopens every write first."""
    from shelfhold.infrastructure.local_socket import Peer, RpcError
    from shelfhold.interfaces.local_cli.dispatch import IdentityDispatcher

    with env(tmp_path) as e:
        ops = OpsServices(e.engine, clock=e.clock, bus=EventBus(e.clock), maintenance=e.ops)
        dispatcher = IdentityDispatcher(None, headless=object(), ops=ops)  # type: ignore[arg-type]
        e.ops.enter(principal=e.principal("cli"))
        code: object = None
        try:
            asyncio.run(dispatcher(Peer(1, 1000, 1000), method, {"username": "root"}))
        except RpcError as error:
            code = (error.data or {}).get("code")
        except Exception:
            code = "past-the-gate"
        assert code != "maintenance.active", method


@pytest.mark.usefixtures("cli")  # the plugin CLI of `test_plugin_cli`, on `settings`
def test_review_poc_the_plugin_cli_writes_while_the_mode_is_on(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """`shelfhold plugin set|set-secret|set-cookies|attest|install|enable` open the database
    themselves (no socket, no `ops_command`): the mode is never read, server up or down."""
    set_mode(SimpleNamespace(settings=settings), True)  # type: ignore[arg-type]
    assert run_cli(monkeypatch, settings, "plugin", "set", KEY, 'lang="en"') == 1
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as conn:
            actions = conn.execute(sa.select(identity.audit.c.action)).scalars().all()
    finally:
        engine.dispose()
    assert "source.settings_changed" not in actions


# -- cancel -------------------------------------------------------------------------------------


def test_review_poc_a_cancel_after_the_work_is_recorded_cancelled(
    tmp_path: Path,
) -> None:
    """No handler but `run_steps` ever reads `stop`: a running job that is "cancelled" does all its
    work, and the ledger says `cancelled`. The replay that follows does the work a second time."""
    with env(tmp_path) as e:
        effects: list[str] = []

        def work(context: JobContext) -> None:
            if not effects:
                e.ops.cancel(context.job_id, principal=e.principal())
            effects.append("downloaded")  # the whole job, stop never looked at

        e.pools.register("acquire.run", pool="io", handler=work)
        job = e.pools.submit("acquire.run", dedupe_key="ch-1").job_id
        e.run()
        assert effects == ["downloaded"]
        assert e.job(job).status == "succeeded", "the work happened; the ledger says cancelled"


def test_review_poc_a_cancel_that_arrives_as_the_job_ends_cancels_its_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker is written while the row is `running`; the worker has already looked
    (`_settle_cancel` False) and ends the row `failed`. The marker stays in `app_setting`, and the
    replay of that job (same row, same id) is stopped and ends `cancelled` on its first run."""
    with env(tmp_path) as e:
        attempts: list[int] = []

        def work(_context: JobContext) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise JobFailedError("job.failed")

        e.pools.register("export.run", pool="io", handler=work)
        job = e.pools.submit("export.run", dedupe_key="e").job_id
        real_settle = e.pools._settle_cancel
        late = {"armed": True}

        def settle_then_cancel(job_id: str) -> bool:
            done = real_settle(job_id)
            if late["armed"]:
                late["armed"] = False
                e.ops.cancel(job_id, principal=e.principal())  # the row is still `running`
            return done

        monkeypatch.setattr(e.pools, "_settle_cancel", settle_then_cancel)
        e.run()
        assert e.job(job).status == "failed"
        assert e.ops.replay(job, principal=e.principal())["replayed"] is True
        e.pools.tick()
        e.run()
        assert e.job(job).status == "succeeded", e.job(job).status
        assert e.rows(platform.app_setting, platform.app_setting.c.key.like("job.cancel.%")) == []


# -- the security repairs -----------------------------------------------------------------------


def test_review_poc_a_stale_session_cancels_a_security_repair(tmp_path: Path) -> None:
    """`priority` refuses to touch a security repair, `cancel` does not: a stolen session (30 days
    sliding, no freshness outside the hardened profile) kills "Mettre à l'abri"."""
    with env(tmp_path) as e:
        ran: list[str] = []
        watch(e, ran, SAFEGUARD, pool="maint")
        repair = e.pools.submit(SAFEGUARD, dedupe_key="repair").job_id
        with pytest.raises(CodedRefusal):
            e.ops.cancel(repair, principal=e.principal("stale"))
        assert e.job(repair).status == "queued"


def test_review_poc_a_global_pause_by_a_stale_session_parks_the_security_repairs(
    tmp_path: Path,
) -> None:
    """`A security repair is above all of them at every moment` (control.py), but `queue pause`
    parks it with the rest, and a session past freshness may do it."""
    with env(tmp_path) as e:
        ran: list[str] = []
        watch(e, ran, SAFEGUARD, pool="maint")
        e.ops.pause(None, principal=e.principal("stale"))
        repair = e.pools.submit(SAFEGUARD, dedupe_key="repair").job_id
        e.run_all()
        assert ran == ["repair"], (e.job(repair).status, e.job(repair).error_code)


def test_review_poc_the_child_exposure_audit_waits_behind_user_jobs(tmp_path: Path) -> None:
    """`target.access_audit` (EF-EXP-055, a child account that sees an adult library) shares the
    `io` pool with acquisitions but is not in the fixed set: it runs at class `new`, the class a
    user may give any job, so it waits behind every one of them."""
    with env(tmp_path) as e:
        ran: list[str] = []
        watch(e, ran, "fetch")
        watch(e, ran, ACCESS_AUDIT)
        for n in range(3):
            e.pools.submit("fetch", dedupe_key=f"user-{n}", priority_class="new")
        e.pools.submit(ACCESS_AUDIT, dedupe_key="audit")
        e.run()
        assert ran[0] == "audit", ran


# -- pause scopes -------------------------------------------------------------------------------


def test_review_poc_pausing_a_source_named_all_pauses_the_whole_queue(tmp_path: Path) -> None:
    """`paused_scopes` strips `source.` from `queue.pause.source.all` and reads `all`: a source
    pause becomes a global one. The source is never checked against the known sources either."""
    with env(tmp_path) as e:
        ran: list[str] = []
        watch(e, ran, "fetch")
        assert e.ops.pause("all", principal=e.principal())["scope"] == "all"
        other = e.pools.submit("fetch", dedupe_key="other", source_key="src-b").job_id
        e.run()
        assert (e.job(other).status, e.job(other).error_code) != ("paused", PAUSE_CODE)
        assert ran == ["other"]
