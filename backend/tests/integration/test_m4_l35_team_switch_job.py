"""The job of the team switch: the lease first, the follow cycle upgrader, the report (M4 L35).

`runtime_jobs.team_switch_handler` is run on a composition that is the test's own: the write
context is the real files of the test, and `_LazyUpgrader` builds the real same-path upgrade on
it. The queue is a recorder.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.team_switch import TeamSwitchService
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.jobs.public import JobContext, JobFailedError
from shelfhold.runtime_jobs import team_switch_handler
from tests.export_env import ExportEnv, export_env
from tests.team_switch_env import add_team_chapter, prefer_another_team, team_switch
from tests.upgrade_env import bin_files

pytestmark = pytest.mark.req("EF-BIB-005", "EF-EXP-041")

ADMIN = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)


class _Pools:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, kind: str, *, dedupe_key: str, **_rest: Any) -> None:
        self.submitted.append(kind)


def _comp(env: ExportEnv, leases: list[str]) -> Any:
    """What the handler asks of the composition root: an engine, a clock, the settings of the
    renderer, the queue, and the write context (its lease is a line in `leases`)."""
    pools = _Pools()

    @contextmanager
    def write_context() -> Iterator[Any]:
        leases.append("taken")
        yield SimpleNamespace(
            fsops=env.lib.fsops, loaded=SimpleNamespace(locate=env.lib.locate), bus=None
        )
        leases.append("released")

    return SimpleNamespace(
        engine=env.engine, clock=env.clock, write_context=write_context, pools=lambda: pools,
        settings=SimpleNamespace(
            data_dir=env.lib.data, page_target_format="jpeg", page_target_quality=90
        ),
        queue=pools,
    )  # fmt: skip


def _context(env: ExportEnv, switch_id: str) -> JobContext:
    return JobContext(
        job_id=new_ulid(), kind="team.switch", dedupe_key=f"team.switch:{switch_id}",
        scope_key="", limit_key=None, window_end=None, cursor=0, clock=env.clock,
        ledger=None,  # type: ignore[arg-type]
    )  # fmt: skip


def _queued(env: ExportEnv, edition_id: str) -> str:
    service = TeamSwitchService(
        env.engine, env.clock, submit=lambda *_a: "01JJOB0000000000000000000A"
    )
    switch_id = str(service.preview(ADMIN, edition_id)["id"])
    service.queue(ADMIN, edition_id, switch_id)
    return switch_id


def test_the_job_takes_the_lease_then_replaces_and_keeps_the_report() -> None:
    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        switch_id = _queued(env, s.edition_id)
        leases: list[str] = []
        comp = _comp(env, leases)
        context = _context(env, switch_id)
        team_switch_handler(comp)(context)
        assert leases == ["taken", "released"]  # one lease, around the whole application
        assert s.target.read_bytes() != old and [p.read_bytes() for p in bin_files(env)] == [old]
        assert context.counters == {"switched": 1, "blocked": 0, "pending": 0}
        assert comp.queue.submitted == ["export.run"]  # what changed goes out
        report = TeamSwitchService(env.engine, env.clock).report(ADMIN, s.edition_id, switch_id)
        assert report["state"] == "done" and report["switched"] == [s.unit_id]


def test_a_job_taken_up_again_after_a_kill_finds_its_work_done() -> None:
    """The job was `applying` when the process died; the boot settled the intents; the queue runs
    it again. A chapter already replaced is up to date: nothing is replaced twice."""
    with export_env() as env:
        s = team_switch(env)
        switch_id = _queued(env, s.edition_id)
        with write_transaction(env.engine) as connection:
            connection.execute(sa.text("UPDATE team_switch SET state = 'applying'"))
        handler = team_switch_handler(_comp(env, []))
        handler(_context(env, switch_id))
        first = s.target.read_bytes()
        with write_transaction(env.engine) as connection:  # killed again before the report
            connection.execute(sa.text("UPDATE team_switch SET state = 'applying', report = NULL"))
        handler(_context(env, switch_id))
        assert s.target.read_bytes() == first and len(bin_files(env)) == 1


def test_a_stale_preview_fails_the_job_with_its_code_and_writes_nothing() -> None:
    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        switch_id = _queued(env, s.edition_id)
        prefer_another_team(env, s)  # nothing the person read holds any more
        comp = _comp(env, [])
        with pytest.raises(JobFailedError) as raised:
            team_switch_handler(comp)(_context(env, switch_id))
        assert raised.value.code == "team_switch.stale"
        assert s.target.read_bytes() == old and comp.queue.submitted == []


def test_a_switch_nobody_knows_fails_the_job_with_the_catalog_code() -> None:
    with export_env() as env:
        team_switch(env)
        with pytest.raises(JobFailedError) as raised:
            team_switch_handler(_comp(env, []))(_context(env, "01JNOSWITCH000000000000000"))
        assert raised.value.code == "team_switch.not_found"


def test_retrying_a_team_switch_job_is_the_reserved_action() -> None:
    from shelfhold.ops.job_actions import reserved_job_action

    with export_env() as env:
        s = team_switch(env)
        job_id = new_ulid()
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO job_view (id, kind, dedupe_key, status) "
                    "VALUES (:i, 'team.switch', 'team.switch:x', 'failed')"
                ),
                {"i": job_id},
            )
        with env.engine.connect() as connection:
            assert reserved_job_action(connection, job_id) == "team.switch"
        assert s.edition_id


def test_a_killed_job_taken_up_again_finishes_the_chapters_it_had_not_reached() -> None:
    """One of two chapters was replaced when the process died; the job runs again from
    `applying` without asking for a new preview, ends the other, and the report names both."""
    from tests.team_switch_env import RealUpgrader, preferred_variant

    with export_env() as env:
        s = team_switch(env)
        series = s.exported.series
        second = env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text("UPDATE unit_variant SET preferred = 1 WHERE unit_id = :u"), {"u": second}
            )
        _variant, _content = add_team_chapter(
            env, s.edition_id, series.binding_id, second, "2", s.team_b, tag=b"two"
        )
        switch_id = _queued(env, s.edition_id)
        # the first run got as far as the first chapter, then died before the second
        RealUpgrader(env).upgrade_unit(s.unit_id, s.content_b)
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text("UPDATE unit_variant SET preferred = 0 WHERE unit_id = :u"),
                {"u": s.unit_id},
            )
            connection.execute(
                sa.text("UPDATE unit_variant SET preferred = 1 WHERE id = :v"), {"v": s.variant_b}
            )
            connection.execute(sa.text("UPDATE team_switch SET state = 'applying'"))
        team_switch_handler(_comp(env, []))(_context(env, switch_id))
        report = TeamSwitchService(env.engine, env.clock).report(ADMIN, s.edition_id, switch_id)
        assert report["state"] == "done" and sorted(report["switched"]) == sorted(
            [s.unit_id, second]
        )
        assert preferred_variant(env, second) == _variant
        assert len(bin_files(env)) == 2  # each old file, once
