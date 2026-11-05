"""The two network adapters of the headless configuration, against simulated servers (L38):
Kavita key exchange (EF-INT-024, ENF-SEC-013) and the Suwayomi authentication-mode probe
(EF-SRC-031). No request leaves the test."""
# ruff: noqa: S105, S106, E501 (synthetic secrets; the tables of arguments read better long)

import json

import httpx2
import pytest

from shelfhold.headless.adapters import HttpKavitaKeyExchange
from shelfhold.headless.errors import TargetExchangeFailed
from shelfhold.infrastructure.fetcher import IntegrationFetcher, LanTargets
from shelfhold.plugin_host.adapters.meta.suwayomi.probe import HttpSuwayomiProbe
from tests.headless_env import KAVITA_KEY, NOW

pytestmark = pytest.mark.req("EF-INT-024")


class Resolver:
    async def resolve(self, host: str, port: int) -> list[str]:
        return ["192.168.1.20"]


def _fetcher(handler: httpx2.MockTransport, host: str, port: int) -> IntegrationFetcher:
    import asyncio

    lan = asyncio.run(LanTargets.declare([f"{host}:{port}"], Resolver()))
    return IntegrationFetcher(Resolver(), lan_targets=lan, transport=handler)


def test_kavita_exchange_logs_in_then_creates_a_named_expiring_key() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path == "/api/Account/login":
            body = json.loads(request.content)
            assert body["username"] == "shelfhold" and body["password"] == "pw-0038"
            return httpx2.Response(200, json={"token": "jwt-0038", "apiKey": "default-key"})
        if request.url.path == "/api/Account/create-auth-key":
            assert request.headers["authorization"] == "Bearer jwt-0038"
            body = json.loads(request.content)
            # RotateAuthKeyRequestDto of the pinned openapi: name, keyLength (8..32), expiresUtc
            assert set(body) == {"name", "keyLength", "expiresUtc"}
            assert body["name"] == "shelfhold" and 8 <= body["keyLength"] <= 32
            assert body["expiresUtc"]
            return httpx2.Response(
                200,
                json={"id": 7, "key": KAVITA_KEY, "name": "shelfhold",
                      "expiresAtUtc": "2027-02-03T12:00:00Z"},
            )  # fmt: skip
        if request.url.path == "/api/Server/server-info-slim":
            return httpx2.Response(200, json={"kavitaVersion": "0.9.1.4"})
        return httpx2.Response(404)

    exchange = HttpKavitaKeyExchange(lambda _u: _fetcher(httpx2.MockTransport(handler), "kavita.internal", 5000),
                                     clock=lambda: NOW)  # fmt: skip
    got = exchange.exchange(url="http://kavita.internal:5000", username="shelfhold",
                            password="pw-0038", key_name="shelfhold", expires_days=90)  # fmt: skip
    assert got.value == KAVITA_KEY and got.version == "0.9.1.4" and got.key_id == 7
    assert got.expires_at is not None
    for request in seen:  # no secret in any URL, and the account key is never used or kept
        assert "pw-0038" not in str(request.url) and KAVITA_KEY not in str(request.url)
        assert "default-key" not in request.content.decode() + str(request.headers)


def test_kavita_revoke_finds_the_old_key_by_value_then_deletes_it_by_id() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path == "/api/Account/login":
            return httpx2.Response(200, json={"token": "jwt-0038"})
        if request.url.path == "/api/Account/auth-keys":
            assert request.headers["authorization"] == "Bearer jwt-0038"
            return httpx2.Response(
                200,
                json=[{"id": 3, "key": "other-key", "name": "shelfhold-20260101"},
                      {"id": 9, "key": KAVITA_KEY, "name": "renamed"}],
            )  # fmt: skip
        if request.url.path == "/api/Account/auth-key":
            return httpx2.Response(200)
        return httpx2.Response(404)

    exchange = HttpKavitaKeyExchange(lambda _u: _fetcher(httpx2.MockTransport(handler), "kavita.internal", 5000))
    assert exchange.revoke(url="http://kavita.internal:5000", username="shelfhold",
                           password="pw-0038", key=KAVITA_KEY, key_name="nope")  # fmt: skip
    deleted = [r for r in seen if r.method == "DELETE"]
    assert [r.url.params["authKeyId"] for r in deleted] == ["9"]
    assert KAVITA_KEY not in "".join(str(r.url) for r in seen)


def test_kavita_revoke_falls_back_to_the_name_and_reports_a_missing_key() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/Account/login":
            return httpx2.Response(200, json={"token": "t"})
        if request.url.path == "/api/Account/auth-keys":
            return httpx2.Response(200, json=[{"id": 4, "key": "x", "name": "shelfhold-20260101"}])
        return httpx2.Response(200)

    def make() -> HttpKavitaKeyExchange:
        return HttpKavitaKeyExchange(lambda _u: _fetcher(httpx2.MockTransport(handler), "kavita.internal", 5000))

    args = {"url": "http://kavita.internal:5000", "username": "u", "password": "pw-0038"}
    assert make().revoke(**args, key="gone", key_name="shelfhold-20260101")
    assert not make().revoke(**args, key="gone", key_name="unknown")


def test_kavita_exchange_refuses_wrong_credentials_without_echoing_them() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"title": "Invalid credentials"})

    exchange = HttpKavitaKeyExchange(lambda _u: _fetcher(httpx2.MockTransport(handler), "kavita.internal", 5000),
                                     clock=lambda: NOW)  # fmt: skip
    with pytest.raises(TargetExchangeFailed) as failed:
        exchange.exchange(url="http://kavita.internal:5000", username="u", password="pw-0038",
                          key_name="shelfhold", expires_days=90)  # fmt: skip
    assert "pw-0038" not in str(failed.value)


def test_suwayomi_probe_reads_the_authentication_mode() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        sent = request.headers.get("authorization")
        if sent is None:
            return httpx2.Response(401)
        return httpx2.Response(200, json={"data": {"settings": {"authMode": "BASIC_AUTH"}}})

    probe = HttpSuwayomiProbe(
        lambda _u: _fetcher(httpx2.MockTransport(handler), "suwayomi.internal", 4567)
    )
    assert (
        probe.auth_mode(url="http://suwayomi.internal:4567", username="a", password="b")
        == "basic_auth"
    )
    # without credentials, a 401 says Basic is required
    assert (
        probe.auth_mode(url="http://suwayomi.internal:4567", username=None, password=None)
        == "basic_auth"
    )


def test_kavita_exchange_dto_matches_the_frozen_openapi() -> None:
    from tests.kavita_env import OPENAPI_DIR

    for name in ("0.9.1", "pinned", "latest"):
        doc = json.loads((OPENAPI_DIR / f"{name}.json").read_text("utf-8"))
        schema = doc["components"]["schemas"]["RotateAuthKeyRequestDto"]
        assert set(schema["required"]) == {"name", "keyLength"}
        assert (
            schema["properties"]["keyLength"]["minimum"]
            <= 32
            <= schema["properties"]["keyLength"]["maximum"]
        )
        assert (
            "/api/Account/create-auth-key" in doc["paths"] and "/api/Account/login" in doc["paths"]
        )


def test_kavita_exchange_turns_a_blocked_dns_into_a_refusal() -> None:
    from shelfhold.infrastructure.fetcher import DnsBlocked

    def blocked(_url: str) -> IntegrationFetcher:
        raise DnsBlocked("kavita.internal is blocked by the DNS resolver")

    exchange = HttpKavitaKeyExchange(blocked, clock=lambda: NOW)
    with pytest.raises(TargetExchangeFailed):
        exchange.exchange(url="http://kavita.internal:5000", username="u", password="pw-0038",
                          key_name="shelfhold", expires_days=90)  # fmt: skip
