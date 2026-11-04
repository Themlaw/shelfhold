"""EF-SRC-023: V1 injects Basic and ApiKey (header, query, path) in the Fetcher, not the plugin."""

import asyncio
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, quote_plus, urlencode

import pytest

from shelfhold.infrastructure.fetcher.errors import InvalidFetchUrl
from shelfhold.infrastructure.logging import forget_secret_values, redact_value
from shelfhold.plugin_host.authflow import AuthInjectingFetcher, UnsupportedAuthFlow
from shelfhold.sdk.fetcher import CookieJarView, DownloadedFile, Response
from shelfhold.sdk.models import AuthApiKey, AuthBasic, AuthForm, AuthInteractive
from shelfhold.sdk.refs import ImageRequest

SECRET = "s3cr3t-value-for-tests"  # noqa: S105


class Recording:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def get(self, url: str, *, headers: dict[str, str] | None = None,
                  params: dict[str, Any] | None = None, referer: str | None = None,
                  cache_ttl: int | None = None) -> Response:  # fmt: skip
        self.calls.append(
            ("GET", url, {"headers": dict(headers or {}), "params": dict(params or {})})
        )
        return Response(status=200)

    async def post(self, url: str, *, data: dict[str, Any] | None = None, json: Any = None,
                   headers: dict[str, str] | None = None) -> Response:  # fmt: skip
        self.calls.append(("POST", url, {"headers": dict(headers or {}), "json": json}))
        return Response(status=200)

    async def download(self, req: ImageRequest) -> DownloadedFile:
        self.calls.append(("DOWNLOAD", req.url, {"headers": dict(req.headers)}))
        return DownloadedFile(sha256="0" * 64, size=0)

    def cookies(self) -> CookieJarView:
        return CookieJarView()


def _fetch(
    flow: Any, secret: dict[str, str], url: str = "http://svc.lan:80/api/{key}/items"
) -> Recording:
    inner = Recording()
    wrapped = AuthInjectingFetcher(inner, flow, secret, origin="http://svc.lan:80")
    asyncio.run(wrapped.get(url))
    asyncio.run(wrapped.post(url, json={"q": 1}))
    asyncio.run(wrapped.download(ImageRequest(url=url)))
    return inner


@pytest.mark.req("EF-SRC-023")
def test_authflow_basic_and_api_key() -> None:
    basic = _fetch(
        AuthBasic(), {"username": "shelfhold", "password": SECRET}, "http://svc.lan:80/x"
    )
    # "shelfhold:s3cr3t-value-for-tests" in base64
    expected = "Basic c2hlbGZob2xkOnMzY3IzdC12YWx1ZS1mb3ItdGVzdHM="
    assert [c[2]["headers"].get("Authorization") for c in basic.calls] == [expected] * 3

    header = _fetch(AuthApiKey(location="header", name="X-Api-Key"), {"api_key": SECRET})
    assert [c[2]["headers"].get("X-Api-Key") for c in header.calls] == [SECRET] * 3

    query = _fetch(AuthApiKey(location="query", name="token"), {"api_key": SECRET})
    assert query.calls[0][2]["params"] == {"token": SECRET}

    path = _fetch(AuthApiKey(location="path", name="key"), {"api_key": SECRET})
    assert [c[1] for c in path.calls] == [f"http://svc.lan:80/api/{SECRET}/items"] * 3


@pytest.mark.req("EF-SRC-023")
def test_authflow_reserved_flows_are_refused_in_v1() -> None:
    for flow in (AuthForm(fields=["u", "p"]), AuthInteractive(reason="captcha")):
        with pytest.raises(UnsupportedAuthFlow):
            AuthInjectingFetcher(Recording(), flow, {}, origin="http://svc.lan:80")


@pytest.mark.req("EF-SRC-023", "ENF-SEC-005")
def test_injecting_fetcher_never_shows_the_secret() -> None:
    wrapped = AuthInjectingFetcher(
        Recording(),
        AuthBasic(),
        {"username": "shelfhold", "password": SECRET},
        origin="http://svc.lan:80",
    )
    assert SECRET not in repr(wrapped) and SECRET not in str(wrapped)


# --- review of PR #37 ------------------------------------------------------------------------
@pytest.fixture
def clean_scrubber() -> Iterator[None]:
    yield
    forget_secret_values()


@pytest.mark.req("EF-SRC-023", "ENF-SEC-001")
@pytest.mark.parametrize(
    "url",
    [
        "http://evil.invalid/x",
        "http://svc.lan:80@evil.invalid/x",  # userinfo trick: the host is evil.invalid
        "http://svc.lan:81/x",
        "https://svc.lan:80/x",
        "ftp://svc.lan:80/x",
        "not a url",
    ],
)
def test_review_credential_only_goes_to_the_declared_origin(url: str) -> None:
    inner = Recording()
    wrapped = AuthInjectingFetcher(
        inner,
        AuthBasic(),
        {"username": "shelfhold", "password": SECRET},
        origin="http://svc.lan:80",
    )
    for call in (
        wrapped.get(url),
        wrapped.post(url, json={}),
        wrapped.download(ImageRequest(url=url)),
    ):
        with pytest.raises(InvalidFetchUrl):
            asyncio.run(call)
    assert inner.calls == []  # nothing left, with or without the header
    # the default port is the same origin
    ok = AuthInjectingFetcher(
        inner, AuthBasic(), {"username": "u", "password": "p"}, origin="http://svc.lan"
    )
    asyncio.run(ok.get("http://SVC.lan:80/x"))
    assert len(inner.calls) == 1


@pytest.mark.req("EF-SRC-023", "ENF-SEC-005")
def test_review_header_value_and_encoded_key_forms_are_scrubbed(clean_scrubber: None) -> None:
    forget_secret_values()
    AuthInjectingFetcher(
        Recording(),
        AuthBasic(),
        {"username": "shelfhold", "password": SECRET},
        origin="http://svc.lan:80",
    )
    header = "Basic c2hlbGZob2xkOnMzY3IzdC12YWx1ZS1mb3ItdGVzdHM="
    scrubbed = str(redact_value(f"Authorization: {header} / {header.split()[1]}"))
    assert "c2hlbGZob2xkOnMz" not in scrubbed

    key = "k/ey with+sp&ce=1"
    AuthInjectingFetcher(
        Recording(), AuthApiKey(location="path", name="key"), {"api_key": key},
        origin="http://svc.lan:80",
    )  # fmt: skip
    for form in (key, quote(key, safe=""), quote_plus(key), urlencode({"k": key})[2:]):
        assert str(redact_value(f"GET /api/{form}/items")).count(form) == 0, form


@pytest.mark.req("EF-SRC-023", "ENF-SEC-005")
def test_review_secret_is_sealed_in_the_wrapper_and_opened_per_call() -> None:
    inner = Recording()
    wrapped = AuthInjectingFetcher(
        inner,
        AuthApiKey(location="header", name="X-Api-Key"),
        {"api_key": SECRET},
        origin="http://svc.lan:80",
    )
    held = repr(vars(wrapped)).encode()
    assert SECRET.encode() not in held and quote(SECRET).encode() not in held
    assert not [v for v in vars(wrapped).values() if isinstance(v, dict | str) and SECRET in str(v)]
    asyncio.run(wrapped.get("http://svc.lan:80/a"))
    asyncio.run(wrapped.get("http://svc.lan:80/b"))
    assert [c[2]["headers"]["X-Api-Key"] for c in inner.calls] == [SECRET, SECRET]
