"""L51 (EF-INT-014, EF-USR-024): `shelfhold intents list|resolve` over an intent that is really
open: the pipeline is cut short in the file-system adapter right after the file was placed, the way
a SIGKILL would leave the journal. The commands are the real ones; so are the routes."""

import re
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.export.application.reconcile import FOREIGN_WRITER
from shelfhold.identity.public import RESERVED_ACTIONS, Effect
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import coherence, export, identity
from tests.auth_env import run_cli
from tests.l50_env import Seeded, Series, seeded
from tests.l51_env import Live as LiveApp
from tests.l51_env import SimulatedKill, killed_after_write, live

pytestmark = pytest.mark.req("EF-INT-014")
KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")


def intents(env: Seeded) -> dict[str, str]:
    return {r.id: r.state for r in env.rows(coherence.fs_intent)}


def the_open_intent(env: Seeded) -> sa.Row[tuple[object, ...]]:
    (row,) = [r for r in env.rows(coherence.fs_intent) if r.state in ("pending", "applying")]
    return row


def kill_the_pipeline(env: Seeded, monkeypatch: pytest.MonkeyPatch) -> sa.Row[tuple[object, ...]]:
    """A real export, cut short after the first file was placed and before its bookkeeping."""
    with killed_after_write(monkeypatch), pytest.raises(SimulatedKill):
        env.export()
    return the_open_intent(env)


def a_foreign_writer_replaced_it(intent: sa.Row[tuple[object, ...]]) -> bytes:
    foreign = b"someone else's file"
    Path(str(intent.dst_path)).write_bytes(foreign)
    return foreign


def test_cli_intents_list_resolve(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """The server is stopped: the command works on the database under the `flock`."""
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        for number in ("1", "2"):
            env.add_chapter(series, number)
        intent = kill_the_pipeline(env, monkeypatch)
        assert (intent.op, intent.state) == ("write", "applying")
        written = Path(str(intent.dst_path))
        assert written.is_file()  # the file is there, its line in the manifest is not
        assert "written" not in {r.state for r in env.rows(export.export_record)}
        capsys.readouterr()

        assert run_cli(monkeypatch, settings, "intents", "list") == 0
        listed = capsys.readouterr().out
        assert intent.id in listed and "write" in listed and "applying" in listed

        assert run_cli(monkeypatch, settings, "intents", "resolve", intent.id) == 0
        assert "completed" in capsys.readouterr().out
        assert intents(env)[intent.id] == "done"
        assert "written" in {r.state for r in env.rows(export.export_record)}
        assert written.is_file()  # completed, not rewritten

        assert run_cli(monkeypatch, settings, "intents", "list") == 0
        assert intent.id not in capsys.readouterr().out  # nothing open is listed any more
        audit = [r for r in env.rows(identity.audit) if r.action == "intents.resolve"]
        assert len(audit) == 1
        assert audit[0].target == f"intent:{intent.id}"
        assert str(audit[0].actor_id).startswith("local_cli:")  # who, from the host CLI
        env.export()  # the rest of the plan goes on: no duplicate, no loss
        assert len(env.exported_files()) == 2


def test_cli_intents_resolve_never_touches_a_drifted_disk_and_abandon_is_typed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with live(monkeypatch, settings, tmp_path) as api:
        intent = kill_the_pipeline(api.env, monkeypatch)
        foreign = a_foreign_writer_replaced_it(intent)
        written = Path(str(intent.dst_path))
        capsys.readouterr()

        # the automatic rule does not guess: a file that is not the expected one stays as it is
        assert run_cli(monkeypatch, settings, "intents", "resolve", intent.id) == 3
        assert "ops.still_waiting" in capsys.readouterr().err
        assert intents(api.env)[intent.id] == "applying"
        assert written.read_bytes() == foreign

        # giving up on an intent lifts its protection of the path: the identifier is typed again
        assert run_cli(monkeypatch, settings, "intents", "resolve", intent.id, "--abandon") == 2
        assert (
            run_cli(
                monkeypatch, settings, "intents", "resolve", intent.id, "--abandon",
                "--confirm", "01JWRONG0000000000000000AA",
            )
            != 0
        )  # fmt: skip
        assert intents(api.env)[intent.id] == "applying"
        assert (
            run_cli(
                monkeypatch, settings, "intents", "resolve", intent.id, "--abandon",
                "--confirm", intent.id,
            )
            == 0
        )  # fmt: skip
        assert intents(api.env)[intent.id] == "cancelled"
        assert written.read_bytes() == foreign  # the disk is never touched by an abandon
        assert run_cli(monkeypatch, settings, "intents", "resolve", intent.id) != 0  # not open

        unknown = run_cli(monkeypatch, settings, "intents", "resolve", "01JNOSUCH0000000000000000A")
        assert unknown != 0


def test_a_closed_intent_is_not_listed_and_resolve_refuses_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with live(monkeypatch, settings, tmp_path) as api:
        api.env.export()  # a complete run leaves only closed intents behind
        assert {state for state in intents(api.env).values()} == {"done"}
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "intents", "list") == 0
        assert "applying" not in capsys.readouterr().out
        closed = next(iter(intents(api.env)))
        assert run_cli(monkeypatch, settings, "intents", "resolve", closed) != 0


def test_intents_resolve_is_a_reserved_action_with_a_fresh_session_never_a_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    action = RESERVED_ACTIONS["intents.resolve"]
    assert action.effects == {Effect.SETTLES_FS_INTENT}
    assert action.needs_fresh_auth(restricted_profile_exists=False, hardened=False)

    state: dict[str, sa.Row[tuple[object, ...]]] = {}

    def cut_short(env: Seeded, _series: Series) -> None:
        state["intent"] = kill_the_pipeline(env, monkeypatch)
        a_foreign_writer_replaced_it(state["intent"])

    with live(monkeypatch, settings, tmp_path, seed=cut_short) as api:
        intent = state["intent"]
        assert intents(api.env)[intent.id] == "applying"  # the boot found it drifted and left it
        listed = api.client.get("/api/v1/intents")
        assert listed.status_code == 200
        assert [i["id"] for i in listed.json()["items"]] == [intent.id]
        url = f"/api/v1/intents/{intent.id}/resolve"
        body = {"action": "abandon", "confirm": intent.id}

        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "ci") == 0
        key = KEY.search(capsys.readouterr().out)
        assert key
        bearer = {"Authorization": f"Bearer {key.group(0)}"}
        refused = api.client.post(url, json=body, headers=bearer)
        assert refused.status_code == 403
        assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
        assert api.client.get("/api/v1/intents", headers=bearer).status_code == 200  # listing: ok
        assert intents(api.env)[intent.id] == "applying"

        api.api.clock.advance(timedelta(minutes=20))  # the session is older than 15 minutes
        stale = api.api.post(url, json=body)
        assert stale.status_code == 401
        assert stale.json()["type"].endswith("reauth-required")
        assert intents(api.env)[intent.id] == "applying"

        assert api.api.login().status_code == 200  # a fresh session
        wrong = api.api.post(url, json={"action": "abandon", "confirm": "nope"})
        assert wrong.status_code == 422 and intents(api.env)[intent.id] == "applying"
        done = api.api.post(url, json=body)
        assert done.status_code == 200, done.text
        assert done.json()["outcome"] == "cancelled"
        assert intents(api.env)[intent.id] == "cancelled"
        audit = [r for r in api.env.rows(identity.audit) if r.target == f"intent:{intent.id}"]
        assert [r.action for r in audit] == ["intents.resolve"]
        assert audit[0].session_id and audit[0].auth_time  # the session and its last password
        again = api.api.post(url, json=body)
        assert again.status_code == 409  # not open any more


def test_the_boot_settles_what_it_can_so_the_listing_is_empty(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """With the server running, the boot has already settled what could be (a killed write whose
    file is the expected one): nothing is left open, and the listing says so."""
    state: dict[str, sa.Row[tuple[object, ...]]] = {}

    def cut_short(env: Seeded, _series: Series) -> None:
        state["intent"] = kill_the_pipeline(env, monkeypatch)

    live_app: LiveApp
    with live(monkeypatch, settings, tmp_path, seed=cut_short) as live_app:
        assert intents(live_app.env)[state["intent"].id] == "done"  # settled by the boot
        assert live_app.client.get("/api/v1/intents").json() == {"items": []}


def test_resolve_is_refused_while_a_job_is_writing_there(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """(review) The intent of a write in progress is not an orphan: the job holds the Work and the
    series folder, so `resolve` (replay or abandon) is refused with nothing changed, and the job
    ends well instead of failing under the feet of an admin."""
    placed, go = threading.Event(), threading.Event()
    original = Fsops.write_atomic

    def slow(self: Fsops, local: str, *args: Any, **kwargs: Any) -> Any:
        done = original(self, local, *args, **kwargs)
        if local.endswith(".cbz") and not placed.is_set():
            placed.set()  # the file is there, its bookkeeping is not: the locks are held
            assert go.wait(30)
        return done

    with live(monkeypatch, settings, tmp_path) as api:
        monkeypatch.setattr(Fsops, "write_atomic", slow)
        plan = api.preview()
        job_id = api.apply(plan.json()["plan_id"], plan.headers["etag"]).json()["job_id"]
        try:
            assert placed.wait(30)
            intent = the_open_intent(api.env)
            before = (intents(api.env), [r.state for r in api.rows(export.export_record)])
            audit = len(api.rows(identity.audit))
            capsys.readouterr()

            assert run_cli(monkeypatch, settings, "intents", "resolve", intent.id) == 3
            assert "ops.still_waiting" in capsys.readouterr().err
            assert (
                run_cli(
                    monkeypatch, settings, "intents", "resolve", intent.id, "--abandon",
                    "--confirm", intent.id,
                )
                == 3
            )  # fmt: skip
            assert (intents(api.env), [r.state for r in api.rows(export.export_record)]) == before
            resolved = [r for r in api.rows(identity.audit) if r.action == "intents.resolve"]
            assert all(r.target != f"intent:{intent.id}" for r in resolved)  # nothing was settled
            assert len(api.rows(identity.audit)) >= audit  # (only attempts may have been logged)
        finally:
            go.set()
        job = api.wait_job(job_id)
        assert job["status"] == "succeeded", job
        assert {r.state for r in api.rows(coherence.fs_intent)} == {"done"}
        assert {r.state for r in api.rows(export.export_record)} == {"written"}
        assert len(api.env.exported_files()) == 2


def reconciled(env: Seeded) -> None:
    _pipeline, reconciler, _fsops = rj.build_pipeline(env.inst.comp, env.scans)
    reconciler.reconcile(env.destination_id)


def test_abandon_with_the_right_file_recognises_it_and_the_unit_is_not_orphaned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """(review) Giving up an intent must not leave its reserved line `planned` forever."""
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        for number in ("1", "2"):
            env.add_chapter(series, number)
        intent = kill_the_pipeline(env, monkeypatch)
        capsys.readouterr()
        args = ("intents", "resolve", intent.id, "--abandon", "--confirm", intent.id)
        assert run_cli(monkeypatch, settings, *args) == 0

        states = [r.state for r in env.rows(export.export_record)]
        assert states == ["written"]  # the file at the target was ours: recognised
        env.export()
        assert sorted(r.state for r in env.rows(export.export_record)) == ["written", "written"]
        assert len(env.exported_files()) == 2
        reconciled(env)
        assert all(r.paused_reason is None for r in env.rows(export.export_series))


def test_abandon_with_a_foreign_file_is_reported_as_a_foreign_writer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """(review) The file at the target is not ours: the line is not silently kept `planned`, the
    reconciliation sees it and pauses the series, and nothing is ever written over it."""
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        for number in ("1", "2"):
            env.add_chapter(series, number)
        intent = kill_the_pipeline(env, monkeypatch)
        foreign = a_foreign_writer_replaced_it(intent)
        capsys.readouterr()
        args = ("intents", "resolve", intent.id, "--abandon", "--confirm", intent.id)
        assert run_cli(monkeypatch, settings, *args) == 0

        reconciled(env)
        assert [r.paused_reason for r in env.rows(export.export_series)] == [FOREIGN_WRITER]
        env.export()  # a paused series receives nothing more
        assert Path(str(intent.dst_path)).read_bytes() == foreign
        assert len(env.exported_files()) == 1
