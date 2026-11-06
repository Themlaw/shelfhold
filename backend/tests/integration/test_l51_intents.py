"""L51 (EF-INT-014, EF-USR-024): `shelfhold intents list|resolve` over an intent that is really
open: the pipeline is cut short in the file-system adapter right after the file was placed, the way
a SIGKILL would leave the journal. The commands are the real ones; so are the routes."""

import re
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import RESERVED_ACTIONS, Effect
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
        audit = [r for r in api.env.rows(identity.audit) if r.action == "intents.resolve"]
        assert [r.target for r in audit] == [f"intent:{intent.id}"]
        assert audit[0].session_id and audit[0].auth_time  # the session and its last password
        again = api.api.post(url, json=body)
        assert again.status_code == 409  # not open any more


def test_replaying_an_open_intent_through_the_api_completes_it(
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
