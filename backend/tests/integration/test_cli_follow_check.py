"""EF-SRC-038 (M2 L29): `shelfhold follow check <series>` and `POST /api/v1/editions/{id}/check`,
the real command and the real route over a real instance."""

from pathlib import Path
from typing import Any

import pytest

from shelfhold.config import Settings
from tests.l51_env import live
from tests.l54_env import Bench54, bench54


class RecordingPools:
    slots = None

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def submit(self, kind: str, **kwargs: Any) -> None:
        self.submitted.append({"kind": kind, **kwargs})


def _edition(b: Bench54) -> str:
    return str(b.rows("SELECT edition_id FROM source_binding")[0]["edition_id"])


@pytest.mark.req("EF-SRC-038")
def test_cli_follow_check_queues_a_news_class_job(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        b.follow()
        pools = RecordingPools()
        b.env.inst.comp.pools = lambda: pools  # type: ignore[method-assign]
        capsys.readouterr()
        assert b.cli("follow", "check", _edition(b)) == 0
        assert "vérification" in capsys.readouterr().out
        (job,) = pools.submitted
        assert job["kind"] == "follow.cycle" and job["priority_class"] == "new"
        row = b.rows("SELECT next_expected FROM source_binding")[0]
        assert row["next_expected"] is not None  # the series is due at the next look


@pytest.mark.req("EF-SRC-038")
def test_cli_follow_check_refuses_what_it_cannot_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        b.follow()
        pools = RecordingPools()
        b.env.inst.comp.pools = lambda: pools  # type: ignore[method-assign]
        assert b.cli("follow", "check", "no-such-edition") != 0
        assert b.cli("follow", "check") == 2  # the series is missing
        b.rows("SELECT 1")
        with b.engine.begin() as connection:
            import sqlalchemy as sa

            connection.execute(sa.text("UPDATE series_follow SET check_override = 'paused'"))
        assert b.cli("follow", "check", _edition(b)) != 0  # paused: not even now
        assert pools.submitted == []
        assert b.rows("SELECT next_expected FROM source_binding")[0]["next_expected"] is None


@pytest.mark.req("EF-SRC-038", "ENF-SEC-010")
def test_check_route_needs_a_session_the_csrf_token_and_the_admin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=()) as api:
        pools = RecordingPools()
        api.env.inst.comp.pools = lambda: pools  # type: ignore[method-assign]
        client = api.client
        import sqlalchemy as sa

        with api.env.inst.engine.connect() as connection:
            edition = connection.execute(sa.text("SELECT id FROM edition LIMIT 1")).scalar_one()
        url = f"/api/v1/editions/{edition}/check"
        assert client.post(url).status_code == 403  # no CSRF token on a session
        anonymous = api.api.other_client("203.0.113.9")
        assert anonymous.post(url).status_code in (401, 403)
        assert pools.submitted == []
        missing = api.api.post("/api/v1/editions/01NOSUCHEDITION00000000000/check")
        assert missing.status_code == 404
        assert pools.submitted == []
