"""L53: what the status tests share. Everything goes through the production doors: `shelfhold
status` is the command, the states are produced by the services that own them (adoption, pairing,
purge, the restore guard, the runtime boot), the exits are the CLI commands of the catalogue."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from tests.auth_env import run_cli
from tests.l54_env import Bench54


def status_json(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "status", "--json") == 0
    return json.loads(capsys.readouterr().out)  # type: ignore[no-any-return]


def status_text(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> str:
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "status") == 0
    return capsys.readouterr().out


def waiting_states(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for item in report["waiting"]:
        found.setdefault(item["state"], []).append(item)
    return found


def age_discoveries(b: Bench54, edition_id: str, days_ago: list[float]) -> None:
    """Time passes: the instants at which the chapters of the Edition were discovered move back
    (the follow cycle stamps `first_seen_at` with the clock; the clock of a real instance is the
    system's, so the past is written where the cycle wrote the present)."""
    now = datetime.now(tz=b.env.inst.comp.clock.now().tzinfo)
    with b.engine.begin() as connection:
        ids = list(
            connection.execute(
                sa.text(
                    "SELECT v.id FROM unit_variant v JOIN unit u ON u.id = v.unit_id "
                    "WHERE u.edition_id = :e AND v.removed_at IS NULL ORDER BY v.id"
                ),
                {"e": edition_id},
            ).scalars()
        )
        assert len(ids) >= len(days_ago), "the series must hold chapters to age"
        for index, variant_id in enumerate(ids):
            when = now - timedelta(days=days_ago[index % len(days_ago)])
            connection.execute(
                sa.text("UPDATE unit_variant SET first_seen_at = :t WHERE id = :i"),
                {"t": when, "i": variant_id},
            )
