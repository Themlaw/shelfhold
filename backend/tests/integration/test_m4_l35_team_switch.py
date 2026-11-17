"""Switching to the preferred team: a preview, then an application under intent (M4 L35).

Real files, a real migrated database and the real same-path upgrade (`SamePathUpgrade`): the click
of the series sheet prepares a preview (served, blocked, pending) that writes nothing; the
application replaces the served files by the preferred team's through the upgrader of the follow
cycle, under an intent, and never replaces an irreplaceable original or an adopted one.
"""

import threading
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.team_switch import (
    StaleSwitchError,
    TeamSwitchRefusedError,
    TeamSwitchService,
    preview_team_switch,
    switch_to_preferred_team,
)
from shelfhold.export.application.intents import IntentJournal
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from tests.export_env import ExportEnv, export_env
from tests.team_switch_env import (
    RealUpgrader,
    Switch,
    add_team_chapter,
    count,
    prefer_another_team,
    preferred_variant,
    team_switch,
    upgrade_intents,
)
from tests.upgrade_env import bin_files, digest, record_of

pytestmark = pytest.mark.req("EF-BIB-005", "EF-BIB-028")

ADMIN = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)


def _preview(env: ExportEnv, edition_id: str):  # type: ignore[no-untyped-def]
    with env.engine.connect() as connection:
        return preview_team_switch(connection, edition_id)


def _apply(env: ExportEnv, s: Switch, token: str | None, upgrader: RealUpgrader | None = None):  # type: ignore[no-untyped-def]
    return switch_to_preferred_team(
        env.engine, s.edition_id, upgrader=upgrader or RealUpgrader(env), token=token
    )


def test_team_switch_preview_then_apply_under_intent() -> None:
    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        before = (upgrade_intents(env), count(env, "export_upgrade"))

        preview = _preview(env, s.edition_id)
        assert preview.to_switch == (s.unit_id,) and preview.blocked == {} and preview.pending == ()
        # a preview writes nothing: not a byte of the file, not a line of the journal
        assert s.target.read_bytes() == old
        assert (upgrade_intents(env), count(env, "export_upgrade")) == before
        assert preferred_variant(env, s.unit_id) != s.variant_b

        upgrader = RealUpgrader(env)
        report = _apply(env, s, preview.token, upgrader)
        assert upgrader.calls == [(s.unit_id, s.content_b)]
        assert report.switched == (s.unit_id,) and report.blocked == {} and report.pending == ()
        # the file at the same path is the preferred team's, the old one is in the bin
        assert env.files() == [s.target]
        assert s.target.read_bytes() != old
        assert [p.read_bytes() for p in bin_files(env)] == [old]
        assert record_of(env, s.exported).file_hash == digest(s.target)
        assert preferred_variant(env, s.unit_id) == s.variant_b
        # under an intent: one `upgrade` intent, done, and the upgrade row that says why
        with env.engine.connect() as connection:
            intents = connection.execute(
                sa.text("SELECT op, state FROM fs_intent WHERE op = 'upgrade'")
            ).all()
            upgrades = connection.execute(
                sa.text("SELECT reason, automatic FROM export_upgrade")
            ).all()
        assert [tuple(i) for i in intents] == [("upgrade", "done")]
        assert [tuple(u) for u in upgrades] == [("better_version", 1)]
        # asked again, nothing is left to switch and nothing is written twice
        again = _apply(env, s, None, upgrader)
        assert again.switched == () and count(env, "export_upgrade") == 1


def test_team_switch_never_replaces_irreplaceable() -> None:
    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        with write_transaction(env.engine) as connection:  # nothing upstream can give it back
            connection.execute(sa.text("UPDATE unit SET availability = 'removed'"))
        preview = _preview(env, s.edition_id)
        assert preview.to_switch == () and preview.blocked == {s.unit_id: ("needs_confirmation",)}
        report = _apply(env, s, preview.token)
        assert report.switched == () and report.blocked == {s.unit_id: ("needs_confirmation",)}
        assert s.target.read_bytes() == old and bin_files(env) == []
        assert preferred_variant(env, s.unit_id) != s.variant_b  # the flag and the file agree
        assert upgrade_intents(env) == 0  # not even an intent: nothing was tried


def test_team_switch_never_replaces_an_adopted_original() -> None:
    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        with write_transaction(env.engine) as connection:  # the file was adopted in place
            connection.execute(sa.text("UPDATE export_record SET state = 'adopted'"))
            connection.execute(sa.text("UPDATE export_item SET kind = 'adopted'"))
        preview = _preview(env, s.edition_id)
        assert preview.blocked == {s.unit_id: ("adopted_original",)} and preview.to_switch == ()
        report = _apply(env, s, preview.token)
        assert report.blocked == {s.unit_id: ("adopted_original",)} and report.switched == ()
        assert s.target.read_bytes() == old and upgrade_intents(env) == 0
        assert preferred_variant(env, s.unit_id) != s.variant_b


def test_team_switch_leaves_a_volume_alone() -> None:
    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        with write_transaction(env.engine) as connection:  # the chapter is part of a volume
            connection.execute(sa.text("UPDATE export_item SET kind = 'volume'"))
        report = _apply(env, s, None)
        assert report.blocked == {s.unit_id: ("assembled",)} and report.switched == ()
        assert s.target.read_bytes() == old and upgrade_intents(env) == 0


def test_team_switch_waits_for_an_open_intent() -> None:
    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)
        journal.record(
            op="upgrade", src=str(s.target), dst=str(s.target), expected_sha256="sha256:" + "0" * 64
        )
        preview = _preview(env, s.edition_id)
        assert preview.blocked == {s.unit_id: ("busy",)}
        report = _apply(env, s, preview.token)
        assert report.blocked == {s.unit_id: ("busy",)} and report.switched == ()
        assert s.target.read_bytes() == old
        assert preferred_variant(env, s.unit_id) != s.variant_b


def test_a_read_chapter_is_blocked_and_left_alone() -> None:
    from tests.upgrade_env import mark_read

    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        mark_read(env, s.exported)
        report = _apply(env, s, None)
        assert report.blocked == {s.unit_id: ("skipped_read",)}
        assert s.target.read_bytes() == old


def test_switch_report_lists_switched_blocked_pending() -> None:
    with export_env() as env:
        s = team_switch(env)
        series = s.exported.series
        # a second chapter that Team B released and fetched, served: it will switch
        second = env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text("UPDATE unit_variant SET preferred = 1 WHERE unit_id = :u"), {"u": second}
            )
        add_team_chapter(env, s.edition_id, series.binding_id, second, "2", s.team_b, tag=b"two")
        # a third one that Team B listed and has not fetched: pending
        third = env.add_chapter(series, "3")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text("UPDATE unit_variant SET preferred = 1 WHERE unit_id = :u"), {"u": third}
            )
        add_team_chapter(env, s.edition_id, series.binding_id, third, "3", s.team_b, fetched=False)
        # and the first one is irreplaceable: blocked
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text("UPDATE unit SET availability = 'removed' WHERE id = :u"), {"u": s.unit_id}
            )
        preview = _preview(env, s.edition_id)
        assert preview.to_switch == (second,)
        assert preview.blocked == {s.unit_id: ("needs_confirmation",)}
        assert preview.pending == (third,)
        report = _apply(env, s, preview.token)
        assert report.switched == (second,)
        assert report.blocked == {s.unit_id: ("needs_confirmation",)}
        assert report.pending == (third,)
        # the three lists never overlap, and the report is what the screen reads
        assert not set(report.switched) & set(report.blocked) & set(report.pending)


def test_an_application_refuses_a_preview_that_went_stale() -> None:
    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        preview = _preview(env, s.edition_id)
        # a chapter arrives between the preview and the application: what the admin read is not
        # what would be done
        extra = env.add_chapter(s.exported.series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text("UPDATE unit_variant SET preferred = 1 WHERE unit_id = :u"), {"u": extra}
            )
        add_team_chapter(env, s.edition_id, s.exported.series.binding_id, extra, "2", s.team_b)
        with pytest.raises(StaleSwitchError):
            _apply(env, s, preview.token)
        assert s.target.read_bytes() == old and upgrade_intents(env) == 0


def test_the_reader_never_sees_a_half_replaced_file() -> None:
    """While the application swaps the file, a reader that opens the path gets the old file or the
    new one, whole: never a short file, never a mix."""
    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        stop, seen, errors = threading.Event(), set(), []

        def reader() -> None:
            while not stop.is_set():
                try:
                    seen.add(s.target.read_bytes())
                except FileNotFoundError:
                    errors.append("the path was empty")
                except OSError as error:  # pragma: no cover
                    errors.append(str(error))
                stop.wait(0.0005)  # a pause: the loop does not starve the application

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            _apply(env, s, None)
        finally:
            stop.set()
            thread.join(timeout=10)
        assert not thread.is_alive()
        new = s.target.read_bytes()
        assert errors == []
        assert seen  # the reader did read
        assert seen <= {old, new}  # only ever one of the two, whole


def test_a_switch_is_a_noop_when_the_preferred_team_already_serves() -> None:
    with export_env() as env:
        s = team_switch(env, prefer=False)  # the priority list is not touched
        assert _preview(env, s.edition_id).to_switch == ()
        assert _apply(env, s, None).switched == ()


# -- the service: the order, its audit line, the report that the screen reads ---------------------


class _Pool:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str]] = []

    def submit(self, kind: str, dedupe_key: str) -> str:
        self.submitted.append((kind, dedupe_key))
        return "01JJOB0000000000000000000A"


def _service(env: ExportEnv) -> tuple[TeamSwitchService, _Pool]:
    pool = _Pool()
    return TeamSwitchService(env.engine, env.clock, submit=pool.submit), pool


def test_the_service_keeps_the_preview_queues_one_job_and_reads_the_report_back() -> None:
    with export_env() as env:
        s = team_switch(env)
        service, pool = _service(env)
        view = service.preview(ADMIN, s.edition_id)
        assert view["to_switch"] == [s.unit_id] and view["state"] == "previewed"
        switch_id = view["id"]
        job_id = service.queue(ADMIN, s.edition_id, switch_id)
        assert pool.submitted == [("team.switch", f"team.switch:{switch_id}")]
        assert job_id == "01JJOB0000000000000000000A"
        assert service.report(ADMIN, s.edition_id, switch_id)["state"] == "queued"
        # the job: the application, then the report the screen reads
        done = service.run(switch_id, upgrader=RealUpgrader(env))
        assert done.switched == (s.unit_id,)
        final = service.report(ADMIN, s.edition_id, switch_id)
        assert final["state"] == "done"
        assert (
            final["switched"] == [s.unit_id] and final["blocked"] == {} and final["pending"] == []
        )
        # the audit trail names the order and the switch, never a path
        with env.engine.connect() as connection:
            audit = connection.execute(
                sa.text(
                    "SELECT action, target FROM audit "
                    "WHERE action LIKE 'team.switch%' ORDER BY at, rowid"
                )
            ).all()
        assert [a[0] for a in audit] == [
            "team.switch_previewed", "team.switch_queued", "team.switch_done",
        ]  # fmt: skip
        assert all(str(a[1]).startswith("team_switch:") for a in audit)
        # the same job twice does nothing the second time
        assert service.run(switch_id, upgrader=RealUpgrader(env)).switched == ()


def test_a_chapter_that_arrived_after_the_preview_is_left_alone() -> None:
    """The application does what the preview said, no more: the chapter that came since is not in
    it (it waits for its own preview), the one that was is switched."""
    with export_env() as env:
        s = team_switch(env)
        service, _pool = _service(env)
        switch_id = service.preview(ADMIN, s.edition_id)["id"]
        service.queue(ADMIN, s.edition_id, switch_id)
        extra = env.add_chapter(s.exported.series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text("UPDATE unit_variant SET preferred = 1 WHERE unit_id = :u"), {"u": extra}
            )
        add_team_chapter(env, s.edition_id, s.exported.series.binding_id, extra, "2", s.team_b)
        (other,) = (p for p in env.files() if p != s.target)
        untouched = other.read_bytes()
        report = service.run(switch_id, upgrader=RealUpgrader(env))
        assert report.switched == (s.unit_id,) and report.blocked == {}
        assert other.read_bytes() == untouched
        assert service.report(ADMIN, s.edition_id, switch_id)["state"] == "done"


def test_a_preview_nothing_of_which_holds_any_more_is_stale_and_writes_nothing() -> None:
    with export_env() as env:
        s = team_switch(env)
        service, _pool = _service(env)
        switch_id = service.preview(ADMIN, s.edition_id)["id"]
        service.queue(ADMIN, s.edition_id, switch_id)
        prefer_another_team(env, s)  # the preferred team is another one: nothing the person read
        old = s.target.read_bytes()
        with pytest.raises(StaleSwitchError):
            service.run(switch_id, upgrader=RealUpgrader(env))
        assert s.target.read_bytes() == old and upgrade_intents(env) == 0
        assert service.report(ADMIN, s.edition_id, switch_id)["state"] == "stale"
        with env.engine.connect() as connection:
            last = (
                connection.execute(
                    sa.text(
                        "SELECT action FROM audit WHERE action LIKE 'team.switch%' ORDER BY rowid"
                    )
                )
                .scalars()
                .all()
            )
        assert last[-1] == "team.switch_stale"


def test_a_failed_run_says_what_it_wrote_and_the_retry_ends_the_rest() -> None:
    """A run that raises on the second chapter: the first is replaced and the report says so
    (never "nothing written"), the audit names the failure, and the retry does the second only."""
    with export_env() as env:
        s = team_switch(env)
        series = s.exported.series
        second = env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text("UPDATE unit_variant SET preferred = 1 WHERE unit_id = :u"), {"u": second}
            )
        add_team_chapter(env, s.edition_id, series.binding_id, second, "2", s.team_b, tag=b"two")
        service, _pool = _service(env)
        switch_id = service.preview(ADMIN, s.edition_id)["id"]
        service.queue(ADMIN, s.edition_id, switch_id)
        real = RealUpgrader(env)

        class _Dies:
            def upgrade_unit(self, unit_id: str, content_id: str) -> list[str]:
                if real.calls:
                    raise TimeoutError("lock held")
                return real.upgrade_unit(unit_id, content_id)

        with pytest.raises(TimeoutError):
            service.run(switch_id, upgrader=_Dies())
        view = service.report(ADMIN, s.edition_id, switch_id)
        assert view["state"] == "failed" and len(view["switched"]) == 1
        assert view["to_switch"] == [u for u in (s.unit_id, second) if u not in view["switched"]]
        retry = RealUpgrader(env)
        report = service.run(switch_id, upgrader=retry)
        assert sorted(report.switched) == sorted([s.unit_id, second])
        assert len(retry.calls) == 1  # the one that was done is not done again
        assert service.report(ADMIN, s.edition_id, switch_id)["state"] == "done"
        with env.engine.connect() as connection:
            actions = (
                connection.execute(
                    sa.text(
                        "SELECT action FROM audit WHERE action LIKE 'team.switch%' ORDER BY rowid"
                    )
                )
                .scalars()
                .all()
            )
        assert actions[-2:] == ["team.switch_failed", "team.switch_done"]


def test_the_service_refuses_a_member_a_child_and_a_key_before_anything() -> None:
    with export_env() as env:
        s = team_switch(env)
        service, pool = _service(env)
        member = Principal("01J00000000000000000000MEM", "mem", "member", Credential.SESSION)
        child = Principal("01J00000000000000000000CHD", "kid", "child", Credential.SESSION)
        key = Principal(
            "01J00000000000000000000KEY", "admin", "admin", Credential.API_KEY, read_only=True
        )
        for who in (member, child, key):
            with pytest.raises(TeamSwitchRefusedError):
                service.preview(who, s.edition_id)
        assert count(env, "team_switch") == 0 and pool.submitted == []


def test_idor_another_edition_or_an_unknown_switch_answers_the_same() -> None:
    with export_env() as env:
        s = team_switch(env)
        other = env.new_series("Another")
        service, pool = _service(env)
        mine = service.preview(ADMIN, s.edition_id)["id"]
        answers = []
        for edition, switch in (
            (other.edition_id, mine),  # a switch of another series
            (s.edition_id, "01JNOSWITCH000000000000000"),  # none at all
            ("01JNOEDITION000000000000000", mine),  # an edition nobody has
        ):
            for call in (service.report, service.queue):
                with pytest.raises(TeamSwitchRefusedError) as raised:
                    call(ADMIN, edition, switch)
                answers.append((raised.value.code, str(raised.value)))
        assert len(set(answers)) == 1  # one answer, whichever it is
        assert "01JNO" not in answers[0][1] and mine not in answers[0][1]
        assert pool.submitted == []
        # an unknown edition cannot be previewed either, with the same answer
        with pytest.raises(TeamSwitchRefusedError) as raised:
            service.preview(ADMIN, "01JNOEDITION000000000000000")
        assert (raised.value.code, str(raised.value)) == answers[0]


def test_a_preview_kept_a_day_is_dropped_by_the_next_one() -> None:
    with export_env() as env:
        s = team_switch(env)
        service, _pool = _service(env)
        first = service.preview(ADMIN, s.edition_id)["id"]
        env.clock.advance(days=2)
        service.preview(ADMIN, s.edition_id)
        with env.engine.connect() as connection:
            ids = [r[0] for r in connection.execute(sa.text("SELECT id FROM team_switch"))]
        assert first not in ids and len(ids) == 1


def test_a_content_adopted_in_place_is_never_replaced() -> None:
    """The record and the item look like any chapter, but the content served is the host's own
    file, adopted by reference (`adopted_ref`): the original is never rewritten."""
    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text(
                    "UPDATE content SET retention = 'adopted_ref' WHERE id IN "
                    "(SELECT content_id FROM export_item)"
                )
            )
        assert _preview(env, s.edition_id).blocked == {s.unit_id: ("adopted_original",)}
        report = _apply(env, s, None)
        assert report.blocked == {s.unit_id: ("adopted_original",)}
        assert s.target.read_bytes() == old and upgrade_intents(env) == 0


def test_the_submitter_queues_as_a_request_and_says_when_the_queue_is_down() -> None:
    from shelfhold.ops.errors import ExitUnavailableError
    from shelfhold.ops.wiring import _submitter

    calls: list[tuple[str, str, str]] = []

    class Pools:
        def submit(self, kind: str, *, dedupe_key: str, priority_class: str) -> Any:
            calls.append((kind, dedupe_key, priority_class))
            return SimpleNamespace(job_id="01JJOB0000000000000000000A")

    running = _submitter(SimpleNamespace(pools=lambda: Pools()))  # type: ignore[arg-type]
    assert running("team.switch", "team.switch:x") == "01JJOB0000000000000000000A"
    assert calls == [("team.switch", "team.switch:x", "request")]
    with pytest.raises(ExitUnavailableError):
        _submitter(SimpleNamespace(pools=lambda: None))("team.switch", "team.switch:x")  # type: ignore[arg-type]
