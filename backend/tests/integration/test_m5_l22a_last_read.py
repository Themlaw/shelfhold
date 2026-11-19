"""M5 L22a (EF-INT-027, debt of M4 #284 m5): the "last read" of a follow is the profile's own.

`follow export` wrote, for each followed series, the highest Unit that anyone had read. A chapter a
child read counted for the OPML of the administrator, and with the sticky rule of the view the
effect was final. It is now one profile's: the one the command is asked for (`--user`), by default
the first administrator.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.follows_export import collect_follows
from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, identity
from tests.auth_env import run_cli
from tests.l51_env import Live, live

pytestmark = pytest.mark.req("EF-INT-027", "EF-LEC-014")


def _user(api: Live, name: str, role: str = "member") -> str:
    user_id = new_ulid()
    with api.env.inst.engine.begin() as connection:
        connection.execute(
            sa.insert(identity.user).values(
                id=user_id, username=name, role=role, can_rate=role == "admin"
            )
        )
    return user_id


def _follow(api: Live) -> None:
    with api.env.inst.engine.begin() as connection:
        connection.execute(
            sa.insert(acquisition.series_follow).values(
                edition_id=api.series.edition_id, work_id=api.series.work_id
            )
        )


def _read(api: Live, user_id: str, unit_id: str) -> None:
    carrier = new_ulid()
    with api.env.inst.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO unit_coverage (carrier_type, carrier_id, unit_id, position, origin) "
                "VALUES ('content', :c, :u, 1, 'manual')"
            ),
            {"c": carrier, "u": unit_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO reading_position (id, user_id, carrier_type, carrier_id, "
                "content_hash, locator, percent, client_ts, server_ts, source) VALUES "
                "(:id, :user, 'content', :c, :hash, '{}', 1.0, :ts, :ts, 'server')"
            ),
            {
                "id": new_ulid(), "user": user_id, "c": carrier, "hash": "sha256:" + "0" * 64,
                "ts": "2026-01-01T00:00:00+00:00",
            },
        )  # fmt: skip


def test_last_read_is_per_profile(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The child read chapter 10, the administrator chapter 2: each export says its own, and the
    other profile's reading never moves it (nor leaks into it)."""
    with live(monkeypatch, settings, tmp_path, chapters=()) as api:
        units = {n: api.env.add_chapter(api.series, n) for n in ("2", "10")}
        _follow(api)
        engine = api.env.inst.engine
        admin, child = _user(api, "root", "admin"), _user(api, "kid")
        _read(api, child, units["10"])
        _read(api, admin, units["2"])

        assert [r.last_number for r in collect_follows(engine, admin)] == ["2"]
        assert [r.last_number for r in collect_follows(engine, child)] == ["10"]
        assert [r.last_number for r in collect_follows(engine, _user(api, "new"))] == [""]


def test_the_command_exports_the_asked_profile_and_by_default_the_first_administrator(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=()) as api:
        units = {n: api.env.add_chapter(api.series, n) for n in ("2", "10")}
        _follow(api)
        admin, child = _user(api, "root", "admin"), _user(api, "kid")
        _read(api, child, units["10"])
        _read(api, admin, units["2"])
        capsys.readouterr()

        assert run_cli(monkeypatch, settings, "follow", "export", "--format", "csv") == 0
        assert capsys.readouterr().out.splitlines()[1].endswith(",2")  # the administrator's
        assert (
            run_cli(monkeypatch, settings, "follow", "export", "--format", "csv", "--user", "kid")
            == 0
        )
        assert capsys.readouterr().out.splitlines()[1].endswith(",10")
        assert (
            run_cli(monkeypatch, settings, "follow", "export", "--user", "nobody") == 1
        )  # an unknown profile is refused, and nothing is written to stdout
        assert capsys.readouterr().out == ""
