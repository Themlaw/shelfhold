"""Secret injection by the Fetcher (EF-SRC-023): V1 implements Basic and ApiKey.

A plugin never reads a secret: the host wraps the Fetcher it hands over, and the wrapper adds the
credential to each request. The secret is held in the wrapper and is never rendered.
"""

import base64
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from sqlalchemy import Connection

from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.plugin_host.errors import UnsupportedAuthFlow
from shelfhold.sdk.fetcher import CookieJarView, DownloadedFile, Fetcher, Response
from shelfhold.sdk.models import AuthApiKey, AuthBasic, AuthFlow
from shelfhold.sdk.refs import ImageRequest

__all__ = ["AuthInjectingFetcher", "UnsupportedAuthFlow", "load_account_secret"]


def load_account_secret(
    connection: Connection, vault: SecretVault, account_id: str
) -> dict[str, str]:
    """Open the sealed secret of a `source_account` row (a JSON object of strings)."""
    plain = vault.open_row(connection, "source_account", account_id)
    loaded = json.loads(plain)
    if not isinstance(loaded, dict):
        raise ValueError("the account secret is not a JSON object")
    return {str(k): str(v) for k, v in loaded.items()}


class AuthInjectingFetcher:
    def __init__(self, inner: Fetcher, flow: AuthFlow, secret: Mapping[str, str]) -> None:
        if not isinstance(flow, AuthBasic | AuthApiKey):
            raise UnsupportedAuthFlow(f"{flow.kind} is reserved: V1 injects basic and api_key")
        self._inner: Any = inner
        self._flow: AuthBasic | AuthApiKey = flow
        self._secret = dict(secret)

    def __repr__(self) -> str:
        return f"AuthInjectingFetcher(flow={self._flow.kind!r})"

    # -- what is injected --------------------------------------------------------------------
    def _value(self) -> str:
        if isinstance(self._flow, AuthBasic):
            pair = f"{self._secret['username']}:{self._secret['password']}"
            return "Basic " + base64.b64encode(pair.encode()).decode()
        return self._secret["api_key"]

    def _apply(
        self, url: str, headers: Mapping[str, str] | None, params: Mapping[str, Any] | None = None
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        out_headers = dict(headers or {})
        out_params = dict(params or {})
        flow = self._flow
        if isinstance(flow, AuthBasic):
            out_headers["Authorization"] = self._value()
        elif flow.location == "header":
            out_headers[flow.name] = self._value()
        elif flow.location == "query":
            out_params[flow.name] = self._value()
        else:
            url = url.replace("{" + flow.name + "}", quote(self._value(), safe=""))
        return url, out_headers, out_params

    @staticmethod
    def _with_query(url: str, params: Mapping[str, Any]) -> str:
        if not params:
            return url
        parts = urlsplit(url)
        query = "&".join(q for q in (parts.query, urlencode(params)) if q)
        return urlunsplit(parts._replace(query=query))

    # -- Fetcher -----------------------------------------------------------------------------
    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
        cache_ttl: int | None = None,
    ) -> Response:
        url, hdrs, prm = self._apply(url, headers, params)
        extra: dict[str, Any] = {"referer": referer} if referer else {}
        if cache_ttl is not None:
            extra["cache_ttl"] = cache_ttl
        response: Response = await self._inner.get(url, headers=hdrs, params=prm, **extra)
        return response

    async def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        url, hdrs, prm = self._apply(url, headers)
        response: Response = await self._inner.post(
            self._with_query(url, prm), data=data, json=json, headers=hdrs
        )
        return response

    async def download(self, req: ImageRequest) -> DownloadedFile:
        url, hdrs, prm = self._apply(req.url, req.headers)
        injected = req.model_copy(update={"url": self._with_query(url, prm), "headers": hdrs})
        downloaded: DownloadedFile = await self._inner.download(injected)
        return downloaded

    def cookies(self) -> CookieJarView:
        view: CookieJarView = self._inner.cookies()
        return view
