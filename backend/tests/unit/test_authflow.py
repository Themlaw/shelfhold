"""EF-SRC-023: V1 injects Basic and ApiKey (header, query, path) in the Fetcher, not the plugin."""

import asyncio
from typing import Any

import pytest
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
    wrapped = AuthInjectingFetcher(inner, flow, secret)
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
            AuthInjectingFetcher(Recording(), flow, {})


@pytest.mark.req("EF-SRC-023", "ENF-SEC-005")
def test_injecting_fetcher_never_shows_the_secret() -> None:
    wrapped = AuthInjectingFetcher(
        Recording(), AuthBasic(), {"username": "shelfhold", "password": SECRET}
    )
    assert SECRET not in repr(wrapped) and SECRET not in str(wrapped)
    assert SECRET not in repr(vars(wrapped)).replace("_secret", "")  # held, never rendered
