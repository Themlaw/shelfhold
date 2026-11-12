"""EF-SRC-038 (M2 L29): `shelfhold follow check <series>` and `POST /api/v1/editions/{id}/check`,
the real command and the real route over a real instance (its queue, its database)."""

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import acquisition
from tests.auth_env import run_cli
from tests.l51_env import Live, live


def _follow(api: Live, *, override: str | None = None) -> str:
    """The seeded series is followed (the seed binds it without a `series_follow` row)."""
    with api.env.inst.engine.begin() as connection:
        connection.execute(
            sa.insert(acquisition.series_follow).values(
                edition_id=api.series.edition_id,
                work_id=api.series.work_id,
                check_override=override,
            )
        )
    return str(api.series.edition_id)


def _jobs(api: Live) -> list[dict[str, object]]:
    with api.env.inst.engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT kind, priority_class FROM job_view WHERE kind = 'follow.cycle'")
        ).mappings()
        return [dict(r) for r in rows]


def _next_expected(api: Live) -> object:
    with api.env.inst.engine.connect() as connection:
        return connection.execute(sa.text("SELECT next_expected FROM source_binding")).scalar()


@pytest.mark.req("EF-SRC-038")
def test_cli_follow_check_queues_a_news_class_job(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=()) as api:
        edition = _follow(api)
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "follow", "check", edition) == 0
        assert "Vérification demandée" in capsys.readouterr().out
        assert {"kind": "follow.cycle", "priority_class": "new"} in _jobs(api)
        assert _next_expected(api) is not None  # due at the next look


@pytest.mark.req("EF-SRC-038")
def test_cli_follow_check_refuses_what_it_cannot_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=()) as api:
        edition = _follow(api, override="paused")
        before = len(_jobs(api))
        assert run_cli(monkeypatch, settings, "follow", "check", "no-such-edition") == 1
        assert "ops.unknown_subject" in capsys.readouterr().err
        assert run_cli(monkeypatch, settings, "follow", "check", edition) == 1  # paused: not now
        assert "ops.invalid_choice" in capsys.readouterr().err
        assert len(_jobs(api)) == before  # nothing was queued
        assert _next_expected(api) is None  # and the series was not made due
        assert run_cli(monkeypatch, settings, "follow", "check", edition, "3") == 2  # a stray one


@pytest.mark.req("EF-SRC-038", "ENF-SEC-010")
def test_check_route_needs_a_session_the_csrf_token_and_the_admin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=()) as api:
        edition = _follow(api)
        before = len(_jobs(api))
        url = f"/api/v1/editions/{edition}/check"
        assert api.client.post(url).status_code == 403  # no CSRF token on a session
        anonymous = api.api.other_client("203.0.113.9")
        assert anonymous.post(url).status_code in (401, 403)
        assert len(_jobs(api)) == before and _next_expected(api) is None

        missing = api.api.post("/api/v1/editions/01NOSUCHEDITION00000000000/check")
        assert missing.status_code == 404
        done = api.api.post(url)
        assert done.status_code == 200, done.text
        assert done.json() == {"queued": 1}
        assert {"kind": "follow.cycle", "priority_class": "new"} in _jobs(api)
        assert _next_expected(api) is not None


@pytest.mark.req("EF-SRC-038")
def test_jobs_run_by_hand_makes_every_series_due(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The cadence is the periodic pace: an admin who runs the follow job asks for a look."""
    with live(monkeypatch, settings, tmp_path, chapters=()) as api:
        _follow(api)
        assert _next_expected(api) is None
        assert run_cli(monkeypatch, settings, "jobs", "run", "follow.cycle") == 0
        assert _next_expected(api) is not None
