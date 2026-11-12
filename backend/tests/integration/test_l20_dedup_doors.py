"""M3 L20: the doors of the duplicate suggestions (admin routes and the ops service). A scan only
records suggestions; a read-only key and a visitor reach none of them."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.ops.dedup import DedupOps
from shelfhold.ops.errors import ExitUnavailableError
from tests.auth_env import AuthEnv, auth_env, run_cli
from tests.catalog_env import database
from tests.integration.test_l20_dedup import Screen

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
A, B = "01JWORK0000000000000000001", "01JWORK0000000000000000002"


class _Dedup:
    def __init__(self) -> None:
        self.scans = 0

    def scan(self) -> dict[str, Any]:
        self.scans += 1
        return {"created": ["01JSUGGEST00000000000000001"]}

    def suggestions(self) -> list[dict[str, str]]:
        return [{"id": "S", "a": A, "b": B, "reason": "same_title_same_author", "state": "open"}]


class _Ops:
    def __init__(self) -> None:
        self.dedup = _Dedup()


@pytest.fixture
def ops() -> _Ops:
    return _Ops()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings, ops: _Ops) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, ops_services=ops) as env:
        yield env


@pytest.mark.req("EF-BIB-004")
def test_suggestion_routes_are_for_an_admin_and_merge_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv, ops: _Ops
) -> None:
    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "ro",
                   "--read-only") == 0  # fmt: skip
    match = KEY.search(capsys.readouterr().out)
    assert match
    read = {"Authorization": f"Bearer {match.group(0)}"}
    scan = "/api/v1/merge-suggestions/works/scan"
    assert api.client.post(scan).status_code == 401
    assert api.client.post(scan, headers=read).status_code == 403
    assert ops.dedup.scans == 0
    api.login()
    assert api.post(scan).json() == {"created": ["01JSUGGEST00000000000000001"]}
    listed = api.client.get("/api/v1/merge-suggestions/works")
    assert listed.status_code == 200 and listed.json()["items"][0]["state"] == "open"
    assert api.client.get("/api/v1/merge-suggestions/works/scan").status_code == 405


class _Screens:
    def __init__(self, screen: Screen) -> None:
        self._screen = screen

    def open(self) -> Screen:
        return self._screen


@pytest.mark.req("EF-BIB-004")
def test_the_ops_scan_records_suggestions_and_fails_closed_without_a_screen() -> None:
    with database() as path:
        engine = create_db_engine(path)
        try:
            with engine.begin() as connection:
                _seed_pair(connection)
            ops = DedupOps(engine, _Screens(Screen()))
            assert len(ops.scan()["created"]) == 1
            assert [s["reason"] for s in ops.suggestions()] == ["same_title_same_author"]
            assert ops.scan()["created"] == []
            with pytest.raises(ExitUnavailableError):
                DedupOps(engine).scan()  # no tombstone screen: no scan
            with engine.connect() as connection:
                assert connection.execute(sa.text("SELECT count(*) FROM work")).scalar_one() == 2
        finally:
            engine.dispose()


def _seed_pair(connection: sa.Connection) -> None:
    from tests.support.catalog import SupportCatalog

    catalog = SupportCatalog(connection)
    for _ in range(2):
        work = catalog.create_work("manga", "La Voie")
        catalog.credit_work(work, "aut", ["Alice Martin"])
