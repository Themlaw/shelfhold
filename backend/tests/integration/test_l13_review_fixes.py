"""Answers to the review of PR #201 (M3 L13) that the PoCs do not already state: the traits of the
kinds, one gate for the commands of the host, the typed pause scopes, the cancel that is told
`too_late`, the database that is busy and is not the mode."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import platform
from shelfhold.interfaces.cli.registry import FROZEN_OPEN, build_parser, label_leaves
from shelfhold.jobs.kinds import DECLARED, security_repair_kinds, traits_of
from shelfhold.jobs.mode import MaintenanceMode
from tests.auth_env import run_cli
from tests.integration.test_maintenance_mode import set_mode
from tests.maintenance_env import env

pytestmark = pytest.mark.req("EF-INT-018", "EF-INT-017")


def test_every_declared_kind_is_a_registered_kind(tmp_path: Path) -> None:
    """A kind renamed (or declared for nothing) leaves the set of repairs silently: the names of
    `jobs.kinds` are the names the production wiring registers."""
    from shelfhold.infrastructure.observability.wiring import register_ops_jobs
    from shelfhold.jobs.integrity_job import register_integrity_job
    from shelfhold.jobs.maintenance import register_sqlite_maintenance
    from shelfhold.runtime_jobs import register_runtime_jobs
    from shelfhold.safeguard_jobs import register_safeguard_job
    from shelfhold.store_jobs import register_store_jobs

    with env(tmp_path) as e:
        comp = MagicMock()
        register_runtime_jobs(e.pools, comp)
        register_store_jobs(e.pools, comp)
        register_safeguard_job(e.pools, comp)
        register_integrity_job(e.pools, comp)
        register_ops_jobs(e.pools, MagicMock(), disk_check=MagicMock(), audit=MagicMock())
        register_sqlite_maintenance(
            e.pools, e.engine, space=MagicMock(), gate=MagicMock(), freeze=MagicMock()
        )
        registered = set(e.pools._handlers)
        assert set(DECLARED) <= registered, set(DECLARED) - registered
        for kind in security_repair_kinds():
            assert traits_of(kind).security_repair


def test_every_leaf_of_the_cli_tree_is_a_read_a_revocation_or_behind_the_gate() -> None:
    parser = build_parser()
    leaves = set(label_leaves(parser))
    assert leaves >= FROZEN_OPEN, (
        f"names of the open list that are no command: {FROZEN_OPEN - leaves}"
    )
    assert len(leaves - FROZEN_OPEN) > 60  # the rest is closed by default, one gate for all
    namespace = parser.parse_args(["plugin", "set", "x", "a=1"])
    assert namespace.command_path == ("plugin", "set")


@pytest.mark.parametrize(
    "words",
    [("adopt", "pair"), ("detach",), ("plugin", "enable", "x"), ("store", "reinit")],
)
def test_the_host_cli_refuses_a_write_while_the_mode_is_on(
    monkeypatch: pytest.MonkeyPatch,
    settings,
    words: tuple[str, ...],
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    from types import SimpleNamespace

    from shelfhold.infrastructure.migrate import migrate

    migrate(settings)
    set_mode(SimpleNamespace(settings=settings), True)  # type: ignore[arg-type]
    assert run_cli(monkeypatch, settings, *words) == 1
    assert "maintenance" in capsys.readouterr().err
    assert run_cli(monkeypatch, settings, "status") == 0  # a read goes through


def test_a_source_called_all_is_a_source_and_the_status_names_both(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        e.ops.pause("all", principal=e.principal())
        e.ops.pause(None, principal=e.principal())
        assert e.ops.status()["queue"]["paused"] == ["all", "source:all"]


def test_cancelling_a_job_that_never_looks_is_told_too_late(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        seen: list[str] = []

        def work(context) -> None:  # type: ignore[no-untyped-def]
            seen.append(e.ops.cancel(context.job_id, principal=e.principal())["outcome"])

        e.pools.register("plain.kind", pool="io", handler=work)
        job = e.pools.submit("plain.kind", dedupe_key="p").job_id
        e.run()
        assert seen == ["too_late"]
        assert e.job(job).status == "succeeded"
        assert e.rows(platform.app_setting, platform.app_setting.c.key.like("job.cancel.%")) == []


def test_a_busy_database_is_not_the_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with env(tmp_path) as e:
        mode = MaintenanceMode(e.engine, e.clock)
        assert mode.probe() is False
        monkeypatch.setattr(
            mode,
            "state",
            MagicMock(side_effect=sa.exc.OperationalError("s", {}, Exception("locked"))),
        )
        assert mode.probe() is None
        assert mode.active() is True  # the safe side for whoever only asks whether a write may go


def test_the_guard_says_db_busy_when_the_flag_cannot_be_read() -> None:
    import asyncio

    from shelfhold.interfaces.http_api.maintenance import MaintenanceGuard

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def receive() -> dict[str, object]:
        return {"type": "http.request"}

    async def app(*_a: object) -> None:  # never reached
        raise AssertionError

    guard = MaintenanceGuard(app, active=lambda: None)  # type: ignore[arg-type]
    scope = {"type": "http", "method": "POST", "path": "/api/v1/x", "headers": [], "state": {}}
    asyncio.run(guard(scope, receive, send))  # type: ignore[arg-type]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")  # type: ignore[misc]
    assert b"db.busy" in body or b"db-busy" in body


def test_a_pause_never_parks_a_security_repair_but_the_mode_does(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        e.pools.register("library.safeguard", pool="maint", handler=lambda c: ran.append("r"))
        e.pools.register("fetch", pool="io", handler=lambda c: ran.append("u"))
        e.ops.pause(None, principal=e.principal("stale"))
        e.pools.submit("fetch", dedupe_key="u")
        e.pools.submit("library.safeguard", dedupe_key="r")
        e.run_all()
        assert ran == ["r"]
        e.ops.enter(principal=e.principal("cli"))
        e.pools.submit("library.safeguard", dedupe_key="r2")
        e.run_all()
        assert ran == ["r"]  # the mode stops everything, repairs included


def test_a_fresh_session_cancels_a_security_repair_and_a_stale_one_cannot(tmp_path: Path) -> None:
    from shelfhold.ops.errors import CodedRefusal

    with env(tmp_path) as e:
        e.pools.register("library.safeguard", pool="maint", handler=lambda c: None)
        job = e.pools.submit("library.safeguard", dedupe_key="r").job_id
        with pytest.raises(CodedRefusal):
            e.ops.cancel(job, principal=e.principal("stale"))
        assert e.ops.cancel(job, principal=e.principal())["outcome"] == "cancelled"
