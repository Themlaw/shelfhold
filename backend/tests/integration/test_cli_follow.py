"""M1b L19 (EF-SRC-029, EF-INT-014): `shelfhold follow` and `unfollow`, the real commands over a
real instance, and the routes `/api/v1/follows` behind the session, the CSRF token and the role."""

from pathlib import Path

import pytest

from shelfhold.config import Settings
from tests.fakes.suwayomi.server import BASE_URL as SUWAYOMI_URL
from tests.fakes.suwayomi.server import PASSWORD, USER
from tests.l51_env import live
from tests.l54_env import Bench54, bench54

GAMMA = ("suwayomi:7003", "/series-gamma")


def in_library(b: Bench54, url: str = GAMMA[1]) -> bool:
    return any(m["url"] == url and m["inLibrary"] for m in b.env.inst.suwayomi.state["mangas"])


@pytest.mark.req("EF-INT-014", "EF-SRC-029")
def test_cli_follow_unfollow(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        server = b.env.inst.suwayomi
        assert not in_library(b)

        assert b.cli("follow", *GAMMA) == 0
        assert "Série suivie" in capsys.readouterr().out
        assert in_library(b)
        assert b.cli("follow", *GAMMA) == 0  # a replay
        assert "déjà suivie" in capsys.readouterr().out
        assert sum(1 for r in server.requests if "updateManga" in r.fields) == 1
        (request,) = b.rows("SELECT state FROM follow_request")
        assert request["state"] == "followed"

        # by default the series stays in Suwayomi
        assert b.cli("unfollow", *GAMMA) == 0
        assert "Série non suivie" in capsys.readouterr().out
        assert in_library(b)

        # leaving the library asks for a confirmation, with the exact command
        asked = len(server.requests)
        assert b.cli("unfollow", "--remove-upstream", *GAMMA) == 3
        assert "--confirm" in capsys.readouterr().err
        assert in_library(b) and len(server.requests) == asked  # nothing was sent
        assert b.cli("unfollow", "--remove-upstream", "--confirm", *GAMMA) == 0
        assert "retirée de la bibliothèque" in capsys.readouterr().out
        assert not in_library(b)
        assert server.violations == []


@pytest.mark.req("EF-SRC-029", "EF-INT-023")
def test_cli_follow_is_refused_while_the_source_is_paused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        server = b.env.inst.suwayomi
        server.assign(
            "/series-alpha", server.add_category("Shelfhold", include_in_download="EXCLUDE")
        )
        b.follow()  # the cycle records the installation
        server.reinstall(wipe=True)
        b.follow(cycles=1)  # and sees another one: paused, `source_instance_changed`
        assert (
            b.rows("SELECT count(*) AS n FROM event WHERE type = 'source.instance_changed'")[0]["n"]
            == 1
        )

        asked = len(server.requests)
        capsys.readouterr()
        assert b.cli("follow", *GAMMA) == 1
        assert "follow.service_paused" in capsys.readouterr().err
        assert len(server.requests) == asked  # not a request left
        assert b.rows("SELECT count(*) AS n FROM follow_request")[0]["n"] == 0
        assert not in_library(b)


@pytest.mark.req("EF-SRC-029", "ENF-SEC-010")
def test_follows_route_needs_a_session_the_csrf_token_and_the_admin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    body = {"source_key": GAMMA[0], "remote_id": GAMMA[1]}
    with live(monkeypatch, settings, tmp_path, chapters=()) as api:
        api.env.inst.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=f"{USER}:{PASSWORD}")
        server = api.env.inst.suwayomi
        client = api.client
        csrf = {"X-CSRF-Token": api.api.csrf()}

        # no CSRF token on a session: refused, nothing sent
        assert client.post("/api/v1/follows", json=body).status_code == 403
        assert client.request("DELETE", "/api/v1/follows", json=body).status_code == 403
        assert not server.requests

        done = api.api.post("/api/v1/follows", json=body)
        assert done.status_code == 200, done.text
        assert done.json() == {"state": "followed", "sent": True}
        again = api.api.post("/api/v1/follows", json=body)
        assert again.json() == {"state": "followed", "sent": False}
        assert sum(1 for r in server.requests if "updateManga" in r.fields) == 1

        kept = client.request("DELETE", "/api/v1/follows", json=body, headers=csrf)
        assert kept.status_code == 200 and kept.json()["state"] == "unfollowed"
        assert any(m["url"] == GAMMA[1] and m["inLibrary"] for m in server.state["mangas"])

        refused = client.request(
            "DELETE", "/api/v1/follows", json={**body, "remove_upstream": True}, headers=csrf
        )
        assert refused.status_code == 409
        assert refused.json()["type"].endswith("follow.confirmation-required")
        assert any(m["url"] == GAMMA[1] and m["inLibrary"] for m in server.state["mangas"])
        removed = client.request(
            "DELETE", "/api/v1/follows", headers=csrf,
            json={**body, "remove_upstream": True, "confirm": True},
        )  # fmt: skip
        assert removed.status_code == 200
        assert not any(m["url"] == GAMMA[1] and m["inLibrary"] for m in server.state["mangas"])

        # no session, no follow
        anonymous = api.api.other_client("203.0.113.9")
        assert anonymous.post("/api/v1/follows", json=body).status_code in (401, 403)
        assert anonymous.request("DELETE", "/api/v1/follows", json=body).status_code in (401, 403)
