"""The export of a profile holds its devices (EF-USR-013, EF-USR-008), and never what opens one.

A device is data of the profile: its name, its kind, when it was paired, last seen and retired.
The token behind it (public identifier, digest, secret) stays out, as in every export.
"""

import json
from collections.abc import Iterator

import pytest

from shelfhold.config import Settings
from tests.auth_env import auth_env
from tests.integration.test_l02_device_review import World

pytestmark = pytest.mark.req("EF-USR-013", "EF-USR-008", "ENF-SEC-013")

FIELDS = {"id", "label", "kind", "created_at", "last_seen_at", "revoked_at"}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with auth_env(monkeypatch, settings) as env:
        yield World(env)


def _export(world: World, who: str) -> tuple[dict[str, object], str]:
    answer = world.send(world.admin, "GET", f"/api/v1/profiles/{world.ids[who]}/export")
    assert answer.status_code == 200, answer.text
    return dict(answer.json()), str(answer.text)


def test_the_export_lists_the_devices_of_the_profile_without_any_secret(world: World) -> None:
    kept = world.paired(world.mum, name="Kobo du salon", kind="kobo")
    gone = world.paired(world.mum, name="Vieux KOReader", kind="koreader")
    assert world.remove(world.mum, gone["device_id"]).status_code == 204
    world.paired(world.admin, "kid", name="Liseuse de l'enfant")  # not hers
    document, text = _export(world, "mum")
    devices = document["devices"]
    assert isinstance(devices, list)
    assert {d["label"] for d in devices} == {"Kobo du salon", "Vieux KOReader"}
    assert all(set(d) == FIELDS for d in devices)
    by_label = {d["label"]: d for d in devices}
    assert by_label["Kobo du salon"]["kind"] == "kobo"
    assert by_label["Kobo du salon"]["revoked_at"] is None
    assert by_label["Vieux KOReader"]["revoked_at"] is not None
    for secret in (kept["secret"], gone["secret"]):
        assert secret not in text
    held = json.dumps(devices)  # the section itself carries no token identifier either
    assert kept["public_id"] not in held and gone["public_id"] not in held
    digests = [str(r[0]) for r in world.rows("SELECT secret_digest FROM device_token")]
    assert digests and all(d not in text for d in digests)
    assert "sha256:" not in text
    assert "secret" not in json.dumps(devices).lower()
