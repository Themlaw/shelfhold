"""M4 L24 (EF-INT-005, ENF-SEC-010): the routes of the webhooks, over the real application.

Registering a webhook sends household data out of the house: the administrator's alone, and a
reserved action (a fresh session, never a key). A child, a member, a read-only key and nobody are
refused on every route, with one answer whether the subscription exists or not. The secret is in
the answer to the creation once, and in no other answer, no log and no row in clear.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.composition import Network
from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from tests.auth_env import ADMIN, CSRF_COOKIE, CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.integration.test_child_surfaces import _routes

BASE = "/api/v1/webhooks"
PUBLIC = "93.184.216.34"
HOOK = "https://hooks.example.org/in/one"
CREATE = {"url": HOOK, "event_types": ["unit.stored"]}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    resolver = FakeResolver(
        {"hooks.example.org": [PUBLIC], "inside.example.org": ["10.0.0.5"],
         "nas.example.lan": ["192.168.1.20"]}
    )  # fmt: skip
    network = Network(resolver=resolver, transport=Recorder().transport)
    with auth_env(monkeypatch, settings, network=network) as env:
        assert env.login(ADMIN).status_code == 200
        yield env


def _send(api: AuthEnv, client: Any, method: str, path: str, body: Any = None) -> Any:
    headers = (
        {}
        if "Authorization" in client.headers
        else {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or "made-up"}
    )
    return client.request(method, path, json=body, headers=headers)


def _key(api: AuthEnv, username: str, **kwargs: Any) -> str:
    engine = create_db_engine(api.settings.database_path)
    try:
        return (
            local_auth(engine, api.settings)
            .create_api_key(name="k", username=username, **kwargs)
            .value
        )
    finally:
        engine.dispose()


def _bearer(api: AuthEnv, key: str, ip: str) -> Any:
    client = api.other_client(ip)
    client.headers["Authorization"] = f"Bearer {key}"
    return client


def _profile(api: AuthEnv, name: str, role: str, ip: str) -> Any:
    auth = api.app.state.auth_gate.auth
    auth.profile_add(name, PASSWORD, role, **({"max_age_rating": 10} if role == "child" else {}))
    client = api.other_client(ip)
    assert api.login(name, client=client).status_code == 200
    return client


def _refused(api: AuthEnv) -> dict[str, Any]:
    """Every reader that is not the administrator's fresh session."""
    return {
        "child": _profile(api, "kid", "child", "10.0.0.2"),
        "member": _profile(api, "ana", "member", "10.0.0.3"),
        "admin_read_key": _bearer(api, _key(api, "admin", read_only=True), "10.0.0.22"),
        "member_key": _bearer(api, _key(api, "ana", read_only=True), "10.0.0.21"),
        "nobody": api.other_client("10.0.0.23"),
    }


def _count(api: AuthEnv) -> int:
    return int(db_rows(api.settings, "SELECT count(*) FROM webhook_subscription")[0][0])


@pytest.mark.req("EF-INT-005", "ENF-SEC-010")
def test_webhook_routes_refused_to_child_member_and_non_admin_key(api: AuthEnv) -> None:
    made = _send(api, api.client, "POST", BASE, CREATE)
    assert made.status_code == 201, made.text
    sub_id = made.json()["id"]
    missing = new_ulid()
    for name, client in _refused(api).items():
        for method, path, body in (
            ("GET", BASE, None),
            ("POST", BASE, CREATE),
            ("GET", f"{BASE}/{sub_id}", None),
            ("DELETE", f"{BASE}/{sub_id}", None),
        ):
            answer = _send(api, client, method, path, body)
            assert answer.status_code in (401, 403), (name, method, path, answer.status_code)
            assert sub_id not in answer.text and "hooks.example.org" not in answer.text, name
        # IDOR: a subscription that exists and one that does not give one answer
        mine = _send(api, client, "GET", f"{BASE}/{sub_id}")
        none = _send(api, client, "GET", f"{BASE}/{missing}")
        assert (mine.status_code, mine.json().get("type")) == (
            none.status_code, none.json().get("type"),
        ), f"{name}: the answer tells whether the subscription exists"  # fmt: skip
        gone = _send(api, client, "DELETE", f"{BASE}/{sub_id}")
        assert gone.status_code in (401, 403), name
    assert _count(api) == 1  # nothing added, nothing removed
    # a key of scope `admin` reads; writing is a reserved action: a fresh session, never a key
    admin_key = _bearer(api, _key(api, "admin"), "10.0.0.24")
    assert admin_key.get(BASE).status_code == 200
    assert _send(api, admin_key, "POST", BASE, CREATE).status_code in (401, 403)
    assert _send(api, admin_key, "DELETE", f"{BASE}/{sub_id}").status_code in (401, 403)
    assert _count(api) == 1


@pytest.mark.req("EF-INT-005", "ENF-SEC-010")
def test_every_webhook_route_is_declared_reserved_or_admin_only(api: AuthEnv) -> None:
    from shelfhold.identity.public import RESERVED_ACTIONS

    assert "webhook.manage" in RESERVED_ACTIONS
    routes = {tuple(entry.split(" ", 1)) for entry in _routes(api.app) if f" {BASE}" in entry}
    assert routes == {
        ("GET", BASE),
        ("POST", BASE),
        ("GET", f"{BASE}/{{webhook_id}}"),
        ("DELETE", f"{BASE}/{{webhook_id}}"),
    }  # fmt: skip


@pytest.mark.req("EF-INT-005", "ENF-SEC-005")
def test_webhook_secret_never_returned_after_creation(api: AuthEnv) -> None:
    made = _send(api, api.client, "POST", BASE, CREATE)
    assert made.status_code == 201
    secret = made.json()["secret"]
    assert len(secret) >= 32 and secret.isprintable() and " " not in secret
    sub_id = made.json()["id"]
    seen = [
        api.client.get(BASE),
        api.client.get(f"{BASE}/{sub_id}"),
        _send(api, api.client, "POST", BASE, {**CREATE, "url": "https://hooks.example.org/two"}),
    ]
    for answer in seen[:2]:
        assert secret not in answer.text and "secret" not in json.dumps(answer.json()).lower()
    listed = seen[0].json()["items"][0]
    assert set(listed) == {
        "id", "url_host", "event_types", "private_target", "enabled", "created_at",
    }  # fmt: skip
    assert "/in/one" not in seen[0].text  # the path of the address may be a token: host only
    assert seen[2].json()["secret"] != secret  # each subscription its own
    # nor in the description of the API
    document = api.app.openapi()
    assert '"secret"' in json.dumps(document)  # the creation answer names it...
    spec = document["components"]["schemas"]
    for name, model in spec.items():
        if "webhook" in name.lower() and "created" not in name.lower():
            assert "secret" not in model.get("properties", {}), name  # ...no other model does
    # the row holds no clear secret, and a replay of the creation never gives it back
    dump = json.dumps(db_rows(api.settings, "SELECT * FROM webhook_subscription"), default=str)
    assert secret not in dump
    audit = json.dumps(db_rows(api.settings, "SELECT * FROM audit"), default=str)
    assert secret not in audit and "/in/one" not in audit


@pytest.mark.req("EF-INT-005", "ENF-SEC-001")
@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest",
        "https://127.0.0.1/hook",
        "https://inside.example.org/hook",
        "https://[::1]/hook",
        "http://hooks.example.org/in/one",
        "https://user:pw@hooks.example.org/in/one",
        "file:///etc/passwd",
    ],
)
def test_registration_refuses_private_and_metadata_addresses(api: AuthEnv, url: str) -> None:
    answer = _send(api, api.client, "POST", BASE, {**CREATE, "url": url})
    assert answer.status_code in (400, 422), answer.text
    assert _count(api) == 0
    assert db_rows(api.settings, "SELECT 1 FROM audit WHERE action LIKE 'webhook.%'") == []
    assert "10.0.0.5" not in answer.text  # the answer does not say what the name resolves to


@pytest.mark.req("EF-INT-005", "ENF-SEC-001")
def test_a_private_target_is_allowed_by_the_admin_for_that_host_and_port(api: AuthEnv) -> None:
    body = {"url": "http://nas.example.lan:8080/hook", "event_types": ["unit.stored"]}
    assert _send(api, api.client, "POST", BASE, body).status_code in (400, 422)
    ok = _send(api, api.client, "POST", BASE, {**body, "allow_private": True})
    assert ok.status_code == 201, ok.text
    assert ok.json()["private_target"] == "nas.example.lan:8080"
    meta = _send(api, api.client, "POST", BASE,
                 {"url": "http://169.254.169.254/", "event_types": ["unit.stored"],
                  "allow_private": True})  # fmt: skip
    assert meta.status_code in (400, 422)  # never, whatever the admin allows
    assert _count(api) == 1


@pytest.mark.req("EF-INT-005")
def test_registration_and_removal_are_audited_without_the_address_or_the_secret(
    api: AuthEnv,
) -> None:
    made = _send(api, api.client, "POST", BASE, CREATE).json()
    assert _send(api, api.client, "DELETE", f"{BASE}/{made['id']}").status_code == 204
    audited = db_rows(
        api.settings, "SELECT action, target FROM audit WHERE action LIKE 'webhook.%' ORDER BY at"
    )
    assert [a[0] for a in audited] == ["webhook.subscribe", "webhook.unsubscribe"]
    dump = json.dumps(audited)
    assert made["secret"] not in dump and "hooks.example.org" not in dump and made["id"] in dump
    assert _send(api, api.client, "DELETE", f"{BASE}/{made['id']}").status_code == 404
    assert _count(api) == 0


@pytest.mark.req("EF-INT-005")
def test_the_event_types_are_a_closed_list(api: AuthEnv) -> None:
    for types in ([], ["auth.passkey_registered"], ["nope"], ["unit.stored"] * 99, "unit.stored"):
        answer = _send(api, api.client, "POST", BASE, {"url": HOOK, "event_types": types})
        assert answer.status_code in (400, 422), types
    assert _count(api) == 0


@pytest.mark.req("EF-INT-005")
def test_the_api_description_documents_the_webhooks(api: AuthEnv) -> None:
    from shelfhold.integrations.webhooks import WEBHOOK_EVENTS

    docs = api.app.openapi()["x-webhooks"]
    assert set(docs["events"]) == WEBHOOK_EVENTS.types()
    delivery = docs["delivery"]
    assert (
        "X-Shelfhold-Signature" in delivery["headers"]
        and "t=" in delivery["headers"]["X-Shelfhold-Signature"]
    )
    assert "180 days" in delivery["deprecation"]  # the double emission is bounded, and says so
    for event in docs["events"].values():
        assert event["current_version"] == 1 and event["deprecated"] == {}
        assert "title" not in event["fields"]["1"]


@pytest.mark.req("EF-INT-005", "ENF-SEC-001")
@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/hook", "http://[::1]:8080/hook"])
def test_the_loopback_is_refused_even_when_the_admin_allows_private(api: AuthEnv, url: str) -> None:
    answer = _send(
        api,
        api.client,
        "POST",
        BASE,
        {"url": url, "event_types": ["unit.stored"], "allow_private": True},
    )
    assert answer.status_code in (400, 422), answer.text
    assert _count(api) == 0
