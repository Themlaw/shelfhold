"""M4 L21a (EF-BIB-013, ENF-LEG-005, ENF-SEC-005): the keys of the providers never reach a browser.

Hardcover and Google Books are asked by the server, with the instance's own key, and by nothing
else. No route, no page and no script of the application names them or lets a browser ask them;
the keys (sealed in `provider_secret`) are read back by no surface at all. The attacks are every
profile that can talk to the API: a child, a member, an API key (read-only or not), the admin
himself, and another profile's identifiers (IDOR) put in every route that takes one. No route is
added by this lot, so none is added to the truth table of reserved actions: the sweep below is
what tells whether one ever leaks a key.
"""

# ruff: noqa: F811  (the `world` fixture is the one of the destination routes)
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.provider_keys import ProviderKey, store_provider_key
from shelfhold.infrastructure.secrets.vault import SecretVault
from tests.auth_env import CSRF_HEADER
from tests.integration import test_fan_out_routes as routes_env
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.integration.test_fan_out_routes import UNKNOWN, World
from tests.redaction_support import synthetic_text
from tests.visibility_env import add_profile

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
HC_KEY = synthetic_text("l21a-browser-hardcover")
GB_KEY = synthetic_text("l21a-browser-googlebooks")
KEYS = (HC_KEY, GB_KEY)
world = routes_env.world
REPO = Path(__file__).resolve().parents[3]
HOSTS = ("hardcover", "googleapis", "books.google")
ROUTE_WORDS = re.compile(r"hardcover|google|provider[-_ ]?keys?|provider[-_ ]?secret", re.I)


def seal_keys(world: World) -> None:
    vault = SecretVault(Keyring.generate(NOW))
    with write_transaction(world.engine) as connection:
        registry.sync_registry(connection, vault.keyring)
        store_provider_key(vault, connection, "hardcover", ProviderKey(HC_KEY))
        store_provider_key(vault, connection, "googlebooks", ProviderKey(GB_KEY))


def leaked(text: str) -> bool:
    return any(key in text for key in KEYS)


def operations(world: World) -> list[tuple[str, str, dict[str, Any]]]:
    spec = world.api.client.get("/openapi.json")
    assert spec.status_code == 200
    found = []
    for path, item in spec.json()["paths"].items():
        for method, operation in item.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                found.append((method, path, operation))
    return found


def readable(world: World) -> list[str]:
    """Every GET route that answers at once (not a stream), with the identifiers of the world put
    in its parameters: its own shelves, an unknown one, and another profile's."""
    paths: list[str] = []
    admin = world.api.client.get("/api/v1/profiles").json()
    others = [p["id"] for p in admin] if isinstance(admin, list) else []
    candidates = [world.adult, world.family, UNKNOWN, *others[:2]]
    for method, path, operation in operations(world):
        streams = any("text/event-stream" in (r.get("content") or {}) for r in operation["responses"].values())
        if method != "get" or streams:
            continue
        if "{" not in path:
            paths.append(path)
            continue
        paths += [re.sub(r"\{[^}]+\}", value, path) for value in candidates]
    return paths


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_hardcover_never_called_from_browser(world: World) -> None:
    """The browser has no road to Hardcover or Google Books: no route of the API names them or
    takes a provider key, the page lets a browser talk to its own server only, and no script of the
    interface knows their hosts."""
    for method, path, operation in operations(world):
        named = " ".join([path, operation.get("operationId", ""), operation.get("summary", "")])
        assert not ROUTE_WORDS.search(named), (method, path)
    page = world.api.client.get("/")
    csp = page.headers.get("content-security-policy", "")
    assert "connect-src 'self'" in csp and not any(h in csp for h in HOSTS)
    for source in (REPO / "frontend" / "src").rglob("*"):
        if source.is_file() and source.suffix in {".ts", ".svelte", ".js", ".css", ".html", ".json"}:
            text = source.read_text("utf-8", errors="ignore").lower()
            assert not any(h in text for h in HOSTS), source.relative_to(REPO)


@pytest.mark.req("ENF-SEC-005", "ENF-LEG-005", "EF-USR-024")
@pytest.mark.parametrize("role", ["child", "member"])
def test_provider_keys_sealed_never_sent_to_browser(world: World, role: str) -> None:
    """A child and a member read every route that answers: whatever they get, the keys are not in
    it, with their own identifiers and with someone else's."""
    seal_keys(world)
    extra = {"max_age_rating": 10} if role == "child" else {}
    client = add_profile(world.api, role, role, "10.0.0.5", **extra)
    for path in readable(world):
        answer = client.get(path)
        assert not leaked(answer.text), (role, path)
        assert not leaked(str(dict(answer.headers))), (role, path)


@pytest.mark.req("ENF-SEC-005", "ENF-LEG-005")
def test_provider_keys_sealed_never_sent_to_the_admin_either(world: World) -> None:
    """The admin is told what is set up, never the keys: the sweep of every readable route."""
    seal_keys(world)
    for path in readable(world):
        answer = world.api.client.get(path)
        assert not leaked(answer.text), path
        assert "secret_ct" not in answer.text and "provider_secret" not in answer.text, path


@pytest.mark.req("ENF-SEC-005", "ENF-LEG-005", "EF-USR-024")
def test_provider_keys_refused_to_an_api_key_read_only_or_not(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seal_keys(world)
    other = world.api.other_client("10.0.0.9")
    for flags in ((), ("--read-only",)):
        key = _create_key(monkeypatch, capsys, world.api.settings, *flags)
        for path in readable(world):
            answer = other.get(path, headers=_bearer(key))
            assert not leaked(answer.text), (flags, path)
        # and no write verb of a key reaches a provider key either: there is no such route
        for method in ("put", "post", "delete"):
            for suffix in ("meta/provider-keys", "providers/hardcover/key", "providers/googlebooks/key"):
                answer = other.request(method, f"/api/v1/{suffix}", headers=_bearer(key))
                assert answer.status_code in {401, 403, 404, 405}, (flags, method, suffix)
                assert not leaked(answer.text)


@pytest.mark.req("ENF-SEC-005", "ENF-LEG-005")
def test_no_route_sets_a_provider_key_for_any_profile(world: World) -> None:
    """Setting a key is the operator's local command, never a request: not for the admin through
    the API, not for a member, not for a child."""
    seal_keys(world)
    member = add_profile(world.api, "mem", "member", "10.0.0.6")
    child = add_profile(world.api, "kid", "child", "10.0.0.7", max_age_rating=10)
    for client in (world.api.client, member, child):
        for method in ("put", "post", "patch"):
            for suffix in ("meta/provider-keys", "providers/hardcover/key", "providers/googlebooks/key",
                           "settings/providers"):
                answer = client.request(method, f"/api/v1/{suffix}", json={"key": synthetic_text("x")},
                                        headers={CSRF_HEADER: world.api.csrf(client)})
                assert answer.status_code in {401, 403, 404, 405, 422}, (method, suffix)
