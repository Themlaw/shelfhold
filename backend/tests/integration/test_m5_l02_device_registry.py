"""M5 L02: the device registry and the rotation of a device's secret (EF-USR-008, EF-USR-022 (4),
EF-USR-024, EF-UX-004, ENF-SEC-013, ADR-0053 §4, D19).

One `device` per pairing: a label, a kind, when it was seen last. Its secret is rotated on the same
device (the old one refused at once, nothing to pair again), and removing a device ends every token
it holds. A member sees and handles the devices of their own profile only, an admin all of them, a
child none; a key or a token handles none. Everything goes through the real application.
"""

import base64
import logging
import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.domain.device_secret import new_device_credential
from shelfhold.infrastructure.logging import REDACTED, redact_value
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env, db_dump, db_rows
from tests.integration.test_l46_device_tokens import KOSYNC_AUTH, OPDS_PATH, basic, kosync, md5
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-USR-008", "EF-USR-022", "ENF-SEC-013")

DEVICES = "/api/v1/devices"
TOKENS = "/api/v1/device-tokens"
SECRET_SHAPE = re.compile(r"^[a-z0-9]{26}$")


class World:
    def __init__(self, env: AuthEnv) -> None:
        self.env = env
        self.admin = env.client
        assert env.login().status_code == 200
        self.mum = add_profile(env, "mum", "member", "10.0.0.2")
        self.dad = add_profile(env, "dad", "member", "10.0.0.3")
        self.kid = add_profile(env, "kid", "child", "10.0.0.4", max_age_rating=10)
        self.ids = {
            name: str(db_rows(env.settings, "SELECT id FROM user WHERE username=?", name)[0][0])
            for name in ("admin", "mum", "dad", "kid")
        }

    def send(self, client: TestClient, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        headers = {CSRF_HEADER: self.env.csrf(client), **kw.pop("headers", {})}
        return client.request(method, path, headers=headers, json=body, **kw)

    def pair(self, client: TestClient, subject: str | None = None, **extra: Any) -> Any:
        body: dict[str, Any] = {**extra}
        if subject is not None:
            body["subject_user_id"] = self.ids[subject]
        return self.send(client, "POST", TOKENS, body)

    def paired(
        self, client: TestClient, subject: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        made = self.pair(client, subject, **extra)
        assert made.status_code == 201, made.text
        return dict(made.json())

    def rotate(self, client: TestClient, device_id: str) -> Any:
        return self.send(client, "POST", f"{DEVICES}/{device_id}/rotate")

    def remove(self, client: TestClient, device_id: str) -> Any:
        return self.send(client, "DELETE", f"{DEVICES}/{device_id}")

    def key(self, username: str) -> str:
        auth = self.env.app.state.auth_gate.auth  # type: ignore[attr-defined]
        return str(
            auth.create_api_key(name="k", username=username, read_only=username != "admin").value
        )

    def kosync_status(self, public_id: str, secret: str) -> int:
        return int(self.admin.get(KOSYNC_AUTH, headers=kosync(public_id, secret)).status_code)

    def opds_status(self, public_id: str, secret: str) -> int:
        return int(self.admin.get(OPDS_PATH, headers=basic(public_id, secret)).status_code)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with auth_env(monkeypatch, settings) as env:
        yield World(env)


@pytest.mark.req("EF-USR-008", "EF-UX-004")
def test_devices_registry(world: World) -> None:
    living = world.paired(world.mum, name="Living room", kind="koreader")
    bedside = world.paired(world.mum, name="Bedside", kind="kobo")
    assert living["device_id"] != bedside["device_id"], "one device per pairing"
    assert world.paired(world.mum)["device_id"], "a kind is optional"
    rows = db_rows(world.env.settings, "SELECT kind FROM device ORDER BY created_at, id")
    assert [kind for (kind,) in rows] == ["koreader", "kobo", "other"]
    assert db_rows(world.env.settings, "SELECT count(*) FROM device_token WHERE device_id IS NULL")[
        0
    ] == (0,), "every token belongs to a device"

    listed = world.mum.get(DEVICES)
    assert listed.status_code == 200
    items = {item["id"]: item for item in listed.json()["items"]}
    assert len(items) == 3
    seen = items[living["device_id"]]
    assert (seen["label"], seen["kind"], seen["subject_user_id"]) == (
        "Living room",
        "koreader",
        world.ids["mum"],
    )
    assert seen["last_seen_at"] is None and seen["revoked_at"] is None
    assert seen["public_id"] == living["public_id"]
    assert "secret" not in seen and "secret_digest" not in seen

    assert world.kosync_status(living["public_id"], living["secret"]) == 200
    after = {i["id"]: i for i in world.mum.get(DEVICES).json()["items"]}
    assert after[living["device_id"]]["last_seen_at"] is not None, "seen for the last time"
    assert after[bedside["device_id"]]["last_seen_at"] is None

    assert world.pair(world.mum, kind="toaster").status_code == 422, "a closed list of kinds"

    # removing a device ends every token it holds, and only its own
    assert world.remove(world.mum, living["device_id"]).status_code == 204
    assert world.kosync_status(living["public_id"], living["secret"]) == 401
    assert world.opds_status(living["public_id"], living["secret"]) == 401
    assert world.kosync_status(bedside["public_id"], bedside["secret"]) == 200
    remaining = {i["id"] for i in world.mum.get(DEVICES).json()["items"]}
    assert living["device_id"] not in remaining and bedside["device_id"] in remaining
    revoked = db_rows(
        world.env.settings,
        "SELECT d.revoked_at IS NOT NULL, t.revoked_at IS NOT NULL FROM device d "
        "JOIN device_token t ON t.device_id = d.id WHERE d.id = ?",
        living["device_id"],
    )
    assert revoked == [(1, 1)]
    assert world.remove(world.mum, living["device_id"]).status_code == 404  # already gone

    # an admin sees every profile's devices, narrowed by `subject_user_id`
    world.paired(world.dad, name="Dad")
    everyone = world.admin.get(DEVICES).json()["items"]
    assert {i["subject_user_id"] for i in everyone} == {world.ids["mum"], world.ids["dad"]}
    only_dad = world.admin.get(DEVICES, params={"subject_user_id": world.ids["dad"]}).json()
    assert {i["subject_user_id"] for i in only_dad["items"]} == {world.ids["dad"]}


@pytest.mark.req("EF-USR-008", "ENF-SEC-013")
def test_a_token_revoked_by_its_old_route_retires_its_device(world: World) -> None:
    made = world.paired(world.mum, name="Old way")
    assert world.send(world.mum, "DELETE", f"{TOKENS}/{made['public_id']}").status_code == 204
    assert made["device_id"] not in {i["id"] for i in world.mum.get(DEVICES).json()["items"]}


@pytest.mark.req("ENF-SEC-013")
def test_url_tokens_hashed_readonly_scoped_rotatable(world: World) -> None:
    made = world.paired(world.mum, kind="koreader")
    secret = made["secret"]
    dump = db_dump(world.env.settings)
    assert secret not in dump and md5(secret) not in dump, "hashed: neither secret nor its md5"
    digests = db_rows(world.env.settings, "SELECT secret_digest FROM device_token")
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", d) for (d,) in digests)

    # read-only on the OPDS door, scoped to /opds and /kosync only
    assert world.opds_status(made["public_id"], secret) == 200
    alone = world.env.other_client("10.0.0.52")
    assert alone.post(OPDS_PATH, headers=basic(made["public_id"], secret)).status_code in (403, 405)
    for path in ("/api/v1/profiles", "/api/v1/stats", DEVICES, TOKENS):
        denied = alone.get(path, headers=basic(made["public_id"], secret))
        assert denied.status_code in (401, 403), path

    # rotatable: the secret changes, the device and its identifier stay
    turned = world.rotate(world.mum, made["device_id"])
    assert turned.status_code == 200, turned.text
    body = turned.json()
    assert body["device_id"] == made["device_id"] and body["public_id"] == made["public_id"]
    assert SECRET_SHAPE.match(body["secret"]) and body["secret"] != secret
    assert turned.headers["cache-control"] == "no-store"
    assert world.opds_status(body["public_id"], body["secret"]) == 200
    assert body["secret"] not in db_dump(world.env.settings)


@pytest.mark.req("EF-USR-008", "ENF-SEC-013")
def test_rotated_secret_refused_immediately(world: World) -> None:
    made = world.paired(world.mum, name="Kobo", kind="kobo", expires_days=30)
    assert world.kosync_status(made["public_id"], made["secret"]) == 200
    turned = world.rotate(world.mum, made["device_id"]).json()
    # no grace period (D19): the old secret is refused on both doors the very next request
    assert world.kosync_status(made["public_id"], made["secret"]) == 401
    assert world.opds_status(made["public_id"], made["secret"]) == 401
    assert world.kosync_status(turned["public_id"], turned["secret"]) == 200
    assert world.opds_status(turned["public_id"], turned["secret"]) == 200
    # the profile is not paired again: same device, same subject, same label, same expiry
    row = db_rows(
        world.env.settings,
        "SELECT d.id, d.label, d.kind, t.subject_user_id, t.expires_at FROM device d "
        "JOIN device_token t ON t.device_id = d.id WHERE t.revoked_at IS NULL",
    )
    assert len(row) == 1 and row[0][:4] == (
        made["device_id"],
        "Kobo",
        "kobo",
        world.ids["mum"],
    )
    assert row[0][4] is not None
    assert db_rows(world.env.settings, "SELECT count(*) FROM device") == [(1,)]
    actions = db_rows(
        world.env.settings, "SELECT action, target FROM audit WHERE action LIKE 'device%'"
    )
    assert ("device_token.rotated", made["device_id"]) in actions
    # a removed device is not rotated back to life
    assert world.remove(world.mum, made["device_id"]).status_code == 204
    assert world.rotate(world.mum, made["device_id"]).status_code == 404
    assert world.kosync_status(turned["public_id"], turned["secret"]) == 401


@pytest.mark.req("EF-USR-008", "EF-USR-022")
def test_rotation_of_an_expired_token_asks_for_a_new_expiry(world: World) -> None:
    made = world.paired(world.mum, expires_days=1)
    world.env.clock.advance(timedelta(days=2))
    assert world.kosync_status(made["public_id"], made["secret"]) == 401
    assert world.env.login("mum", client=world.mum).status_code == 200  # a fresh session again
    stale = world.send(world.mum, "POST", f"{DEVICES}/{made['device_id']}/rotate", {})
    assert stale.status_code == 422 and stale.json()["reason"] == "expiry_required"
    fresh = world.send(
        world.mum, "POST", f"{DEVICES}/{made['device_id']}/rotate", {"expires_days": 30}
    )
    assert fresh.status_code == 200, fresh.text
    assert world.kosync_status(fresh.json()["public_id"], fresh.json()["secret"]) == 200


@pytest.mark.req("EF-USR-008", "EF-USR-022", "ENF-SEC-010")
def test_member_cannot_list_or_rotate_another_profile_device(world: World) -> None:
    mine = world.paired(world.mum, name="Mum's")
    theirs = world.paired(world.dad, name="Dad's")
    # the list is the member's own, whatever they ask for
    own = world.mum.get(DEVICES).json()["items"]
    assert [i["id"] for i in own] == [mine["device_id"]]
    asked = world.mum.get(DEVICES, params={"subject_user_id": world.ids["dad"]})
    assert asked.status_code == 403 and theirs["device_id"] not in asked.text
    # IDOR: another profile's device answers as a device that is not there, and nothing moves
    unknown = world.rotate(world.mum, "0" * 26)
    stolen = world.rotate(world.mum, theirs["device_id"])
    assert stolen.status_code == unknown.status_code == 404
    assert stolen.json()["code"] == unknown.json()["code"] == "device_token_not_found"
    assert world.remove(world.mum, theirs["device_id"]).status_code == 404
    assert world.kosync_status(theirs["public_id"], theirs["secret"]) == 200, "untouched"
    # an admin may, for any profile
    done = world.rotate(world.admin, theirs["device_id"])
    assert done.status_code == 200
    assert world.kosync_status(theirs["public_id"], theirs["secret"]) == 401
    # a key or a token manages no device, whatever the profile
    owner = world.paired(world.mum)
    alone = world.env.other_client("10.0.0.50")
    for key in (world.key("admin"), world.key("mum")):
        bearer = {"Authorization": f"Bearer {key}"}
        for method, path in (
            ("POST", f"{DEVICES}/{owner['device_id']}/rotate"),
            ("DELETE", f"{DEVICES}/{owner['device_id']}"),
        ):
            assert alone.request(method, path, headers=bearer).status_code in (401, 403)
    token_auth = basic(owner["public_id"], owner["secret"])
    sent = alone.post(f"{DEVICES}/{owner['device_id']}/rotate", headers=token_auth)
    assert sent.status_code in (401, 403)
    assert world.kosync_status(owner["public_id"], owner["secret"]) == 200


@pytest.mark.req("EF-USR-008", "EF-USR-022", "ENF-SEC-010")
def test_child_cannot_pair_or_rotate(world: World) -> None:
    by_admin = world.paired(world.admin, "kid", name="Kid's e-reader")
    assert world.pair(world.kid).status_code == 403
    assert world.pair(world.kid, "kid").status_code == 403
    assert world.kid.get(DEVICES).status_code == 403
    assert world.rotate(world.kid, by_admin["device_id"]).status_code == 403
    assert world.remove(world.kid, by_admin["device_id"]).status_code == 403
    alone = world.env.other_client("10.0.0.51")
    bearer = {"Authorization": f"Bearer {world.key('kid')}"}
    sent = alone.post(f"{DEVICES}/{by_admin['device_id']}/rotate", headers=bearer)
    assert sent.status_code in (401, 403)
    # the child's own e-reader token manages nothing either
    as_kid = basic(by_admin["public_id"], by_admin["secret"])
    one = f"{DEVICES}/{by_admin['device_id']}"
    for method, path in (("GET", DEVICES), ("POST", f"{one}/rotate"), ("DELETE", one)):
        sent = alone.request(method, path, headers=as_kid)
        assert sent.status_code in (401, 403)
    assert world.kosync_status(by_admin["public_id"], by_admin["secret"]) == 200


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_the_rotation_needs_a_fresh_session_and_the_removal_the_rule_of_revocations(
    world: World,
) -> None:
    """Rotating shows a new secret: a fresh session in every profile, like pairing. Retiring a
    device is a revocation: the rule of the other credential actions (fresh only in the hardened
    profile), and never a key or a token (tested above)."""
    made = world.paired(world.mum)
    world.env.clock.advance(timedelta(seconds=1200))  # the session of the member is not fresh
    stale = world.rotate(world.mum, made["device_id"])
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    assert world.kosync_status(made["public_id"], made["secret"]) == 200, "nothing moved"
    assert world.remove(world.mum, made["device_id"]).status_code == 204
    assert world.kosync_status(made["public_id"], made["secret"]) == 401


@pytest.mark.req("ENF-SEC-013")
def test_device_secret_never_logged(
    world: World, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    made = world.paired(world.mum, name="Kobo", kind="kobo")
    wrong = "q" * 26
    world.kosync_status(made["public_id"], made["secret"])
    world.kosync_status(made["public_id"], wrong)
    world.opds_status(made["public_id"], wrong)
    turned = world.rotate(world.mum, made["device_id"]).json()
    world.kosync_status(turned["public_id"], turned["secret"])
    world.remove(world.mum, made["device_id"])
    seen = capsys.readouterr()
    everything = seen.out + seen.err + caplog.text + db_dump(world.env.settings)
    forms = (
        made["secret"], md5(made["secret"]), turned["secret"], md5(turned["secret"]), wrong,
        md5(wrong), base64.b64encode(f"{made['public_id']}:{wrong}".encode()).decode(),
    )  # fmt: skip
    for form in forms:
        assert form not in everything
    actions = [a for (a,) in db_rows(world.env.settings, "SELECT action FROM audit")]
    assert "device_token.rotated" in actions and "device.removed" in actions


@pytest.mark.req("ENF-SEC-013")
def test_the_redaction_filter_masks_the_device_credentials_in_a_line() -> None:
    typed = new_device_credential().secret  # drawn at each run: no literal secret
    key = md5(typed)
    line = redact_value(
        {
            "headers": {
                "x-auth-key": key,
                "x-auth-user": "abcd1234",
                "authorization": "Basic Zm9v",
            },
            "event": f"kosync x-auth-key: {key} secret={typed}",
        }
    )
    text = str(line)
    assert key not in text and typed not in text and "Zm9v" not in text
    assert REDACTED in text


@pytest.mark.req("ENF-SEC-008")
@pytest.mark.parametrize("path", ["/opds/series", "/kosync/users/auth", "/opds", "/kosync"])
def test_opds_and_kosync_are_rate_limited_per_address(path: str) -> None:
    """M3 L06's limit of 120 requests a minute holds on the two doors of an e-reader (the routes
    L46 and L47 gave): the 121st request of an address is a 429 with a wait."""
    import asyncio

    from shelfhold.interfaces.http_api.ratelimit import PublicRateLimit

    served: list[int] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        served.append(1)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    limited = PublicRateLimit(app, per_minute=120, clock=lambda: 1000.0)
    statuses: list[int] = []

    async def call() -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        scope = {
            "type": "http", "method": "GET", "path": path, "headers": [], "query_string": b"",
            "client": ("198.51.100.77", 40000), "state": {}, "scheme": "https",
        }  # fmt: skip
        await limited(scope, receive, send)

    for _ in range(121):
        asyncio.run(call())
    assert statuses[:120] == [204] * 120 and statuses[120] == 429
    assert len(served) == 120


@pytest.mark.req("EF-USR-008", "ENF-SEC-013")
def test_a_token_under_a_retired_device_is_refused(world: World) -> None:
    """Defence in depth (#347 MIN-2): every writer retires a device and its tokens together, but
    the check of a token reads the device too: a live token under a retired device opens nothing."""
    made = world.paired(world.mum, kind="kobo")
    assert world.kosync_status(made["public_id"], made["secret"]) == 200
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(world.env.settings.database_path, isolation_level=None)) as conn:
        conn.execute(
            "UPDATE device SET revoked_at = CURRENT_TIMESTAMP WHERE id = ?", (made["device_id"],)
        )
    assert db_rows(
        world.env.settings,
        "SELECT revoked_at IS NULL FROM device_token WHERE public_id=?",
        made["public_id"],
    ) == [(1,)], "the token alone is still live"
    assert world.kosync_status(made["public_id"], made["secret"]) == 401
    assert world.opds_status(made["public_id"], made["secret"]) == 401
