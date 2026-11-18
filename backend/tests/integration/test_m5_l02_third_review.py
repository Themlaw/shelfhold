"""Troisième relecture de M5 L02 : deux mineurs, rouges d'abord.

1. Le jeton d'un flux ouvert (téléchargement d'un lot de série) doit être jugé par le même
   prédicat que la porte : un appareil retiré n'ouvre rien, et ne garde rien d'ouvert.
2. La révocation par l'ancienne route lit puis écrit : sous une transaction différée, une rotation
   qui valide entre les deux donnait un 500 (`SQLITE_BUSY_SNAPSHOT`) au lieu d'un 204 ou d'un 404.
"""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from shelfhold.config import Settings
from shelfhold.identity.public import device_token_is_live
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.reading.bundle.service import SeriesBundles
from tests.auth_env import auth_env
from tests.integration.test_l02_device_review import TOKENS, World

pytestmark = pytest.mark.req("EF-USR-008", "ENF-SEC-013")

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with auth_env(monkeypatch, settings) as env:
        yield World(env)


def test_one_predicate_judges_a_device_token_revocation_device_and_expiry() -> None:
    later, earlier = NOW + timedelta(days=1), NOW - timedelta(days=1)
    assert device_token_is_live(None, None, None, NOW)
    assert device_token_is_live(None, None, later, NOW)
    assert not device_token_is_live(earlier, None, None, NOW)  # token revoked
    assert not device_token_is_live(None, earlier, None, NOW)  # device retired
    assert not device_token_is_live(None, None, NOW, NOW)  # expiry reached
    assert not device_token_is_live(None, None, earlier, NOW)


def test_an_open_download_stops_when_only_its_device_is_retired(world: World) -> None:
    """The stream re-asks `_token_alive` as it goes: the token row is still live, the device of
    the pairing is retired — the gate refuses it, the stream must end as well."""
    made = world.paired(world.mum, name="Kobo")
    token_id = str(
        world.rows("SELECT id FROM device_token WHERE public_id = ?", made["public_id"])[0][0]
    )
    engine = create_db_engine(world.env.settings.database_path)
    bundles = SeriesBundles(engine, lambda: None, now=lambda: NOW)  # type: ignore[arg-type,return-value]
    alive = bundles._token_alive(token_id)
    assert alive()
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE device SET revoked_at = :now WHERE id = :id"),
            {"now": NOW, "id": made["device_id"]},
        )
    assert not alive()


def test_the_old_route_revoke_takes_the_write_lock_first(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revocation reads the device, then writes it: a deferred transaction fails with a stale
    snapshot (500) when a rotation commits between the two. Like `retire_device`, it opens with
    `BEGIN IMMEDIATE`: the rotation waits, then finds the device gone (404)."""
    made = world.paired(world.mum, name="Kobo")
    store = world.env.app.state.auth_gate.auth._store  # type: ignore[attr-defined]
    real = store.revoke_device_token
    inside = threading.local()
    openings: list[str] = []

    def revoking(*args: Any, **kwargs: Any) -> Any:
        inside.on = True
        try:
            return real(*args, **kwargs)
        finally:
            inside.on = False

    monkeypatch.setattr(store, "revoke_device_token", revoking)

    @event.listens_for(store._engine, "begin")
    def opened(conn: Any) -> None:
        if getattr(inside, "on", False):
            openings.append(str(conn.get_execution_options().get("shelfhold_begin", "BEGIN")))

    try:
        answer = world.send(world.mum, "DELETE", f"{TOKENS}/{made['public_id']}")
    finally:
        event.remove(store._engine, "begin", opened)
    assert answer.status_code == 204, answer.text
    assert openings == ["BEGIN IMMEDIATE"]
