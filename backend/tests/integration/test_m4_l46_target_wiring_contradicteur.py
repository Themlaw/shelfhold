"""#342 : contradicteur (M4 L46, relecture adverse secrets, SSRF, autorisation).

Chaque PoC est un `xfail(strict=True)` : il passe au vert le jour où le trou est fermé.
"""

# ruff: noqa: F811  (the `world` fixture of L06a is imported, then asked for by name)
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.targets.komga import load_komga_key
from shelfhold.headless import HeadlessError
from shelfhold.infrastructure.schema import export, identity
from tests.fakes.komga import API_KEY, BASE_URL, OTHER_KEY, FakeKomga
from tests.headless_env import Headless, headless
from tests.integration.test_fan_out_routes import World, world  # noqa: F401
from tests.integration.test_m4_l46_target_wiring import _probe, env, komga_http  # noqa: F401

KOMGA = "/api/v1/targets/komga"


def _audit_rows(engine: sa.Engine) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(sa.select(identity.audit)))


def _path_map(engine: sa.Engine, name: str) -> list[Any]:
    table = export.export_target
    with engine.connect() as connection:
        row = connection.execute(sa.select(table.c.path_map).where(table.c.name == name)).one()
    return list(row.path_map)


def test_poc_a_path_map_change_through_the_komga_door_is_journaled(
    world: World, komga_http: FakeKomga
) -> None:
    body = {"name": "Salon", "url": BASE_URL, "api_key": API_KEY}
    first = world.api.post(KOMGA, json=body)
    assert first.status_code == 201, first.text
    other = world.library.parent / "elsewhere"
    other.mkdir(exist_ok=True)
    new_map = [{"local": str(other), "remote": "/elsewhere", "mode": "rw"}]
    before = len(_audit_rows(world.engine))
    again = world.api.post(KOMGA, json={**body, "path_map": new_map})
    assert again.status_code in (200, 201), again.text
    assert _path_map(world.engine, "Salon")[0]["local"] == str(other)  # the folders did change
    after = _audit_rows(world.engine)[before:]
    assert after, "the folders Shelfhold writes changed and the journal says nothing"


@pytest.fixture
def komga_env(settings: Settings) -> Iterator[tuple[Headless, FakeKomga]]:
    fake = FakeKomga(keys={API_KEY: True, OTHER_KEY: False})
    with headless(settings, komga=_probe(fake)) as env:
        yield env, fake


def test_poc_a_new_key_for_an_existing_komga_is_not_silently_dropped(
    komga_env: tuple[Headless, FakeKomga], tmp_path: Path
) -> None:
    env, fake = komga_env
    first = env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=API_KEY)
    fake.requests.clear()
    try:
        env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=OTHER_KEY)
    except HeadlessError:
        return  # refused (conflict, or probed and refused): the operator is told
    with env.engine.connect() as connection:
        kept = load_komga_key(env.vault, connection, first.id).reveal()
    # either the new key was probed and sealed, or the answer must not be « already there »
    assert fake.requests and kept == OTHER_KEY
