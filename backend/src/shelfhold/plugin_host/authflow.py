"""Secret injection by the Fetcher (EF-SRC-023): V1 implements Basic and ApiKey.

A plugin never reads a secret: the host wraps the Fetcher it hands over, and the wrapper adds the
credential to each request. The secret is held in the wrapper and is never rendered.
"""

import base64
import json
import secrets
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, quote_plus, urlencode, urlsplit, urlunsplit

from sqlalchemy import Connection

from shelfhold.infrastructure.fetcher.errors import InvalidFetchUrl
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.secrets.envelope import Envelope, open_with, seal
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


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        scheme, host, port = parts.scheme.lower(), (parts.hostname or "").lower(), parts.port
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    return scheme, host, port or _DEFAULT_PORTS[scheme]


class AuthInjectingFetcher:
    """Wraps a Fetcher and adds the credential, only towards the declared origin.

    The secret stays sealed in the wrapper under an ephemeral in-process key and is opened for
    each call, so no clear-text dictionary lives as long as the wrapper does. Every form of the
    credential that can reach a log line is registered for scrubbing.
    """

    has_credentials = True

    def __init__(
        self, inner: Fetcher, flow: AuthFlow, secret: Mapping[str, str], *, origin: str
    ) -> None:
        if not isinstance(flow, AuthBasic | AuthApiKey):
            raise UnsupportedAuthFlow(f"{flow.kind} is reserved: V1 injects basic and api_key")
        declared = _origin(origin)
        if declared is None:
            raise InvalidFetchUrl("the origin of an authenticated service must be http(s)://host")
        self._inner: Any = inner
        self._flow: AuthBasic | AuthApiKey = flow
        self._origin = declared
        self._key = secrets.token_bytes(32)
        self._row = secrets.token_hex(8)
        self._sealed: Envelope = seal(
            self._key, "ephemeral", json.dumps(dict(secret)).encode(),
            table="authflow", column="secret", row_id=self._row,
        )  # fmt: skip
        self._register(dict(secret))

    def __repr__(self) -> str:
        return f"AuthInjectingFetcher(flow={self._flow.kind!r})"

    def _open(self) -> dict[str, str]:
        plain = open_with(
            self._key, self._sealed, table="authflow", column="secret", row_id=self._row
        )
        loaded: dict[str, str] = json.loads(plain)
        return loaded

    def _register(self, secret: dict[str, str]) -> None:
        """The exact header value and the encoded forms of a key placed in the path or query."""
        flow = self._flow
        value = self._value(secret)
        if isinstance(flow, AuthBasic):
            register_secret_value(value)
            register_secret_value(value.removeprefix("Basic "))
            register_secret_value(secret["password"])
            return
        for form in (value, quote(value, safe=""), quote_plus(value), urlencode({"k": value})[2:]):
            register_secret_value(form)

    # -- what is injected --------------------------------------------------------------------
    def _value(self, secret: Mapping[str, str]) -> str:
        if isinstance(self._flow, AuthBasic):
            pair = f"{secret['username']}:{secret['password']}"
            return "Basic " + base64.b64encode(pair.encode()).decode()
        return secret["api_key"]

    def _apply(
        self, url: str, headers: Mapping[str, str] | None, params: Mapping[str, Any] | None = None
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        if _origin(url) != self._origin:
            raise InvalidFetchUrl("the credential is only sent to the declared service origin")
        out_headers = dict(headers or {})
        out_params = dict(params or {})
        flow = self._flow
        value = self._value(self._open())
        if isinstance(flow, AuthBasic):
            out_headers["Authorization"] = value
        elif flow.location == "header":
            out_headers[flow.name] = value
        elif flow.location == "query":
            out_params[flow.name] = value
        else:
            url = url.replace("{" + flow.name + "}", quote(value, safe=""))
        return url, out_headers, out_params

    @staticmethod
    def _with_query(url: str, params: Mapping[str, Any]) -> str:
        if not params:
            return url
        parts = urlsplit(url)
        query = "&".join(q for q in (parts.query, urlencode(params)) if q)
        return urlunsplit(parts._replace(query=query))

    def authorize(
        self, url: str, headers: Mapping[str, str] | None = None
    ) -> tuple[str, dict[str, str]]:
        """The URL and headers of a request carrying the credential, for a streamed download that
        does not go through `download` (the bounded receiver of the library). Same origin rule."""
        out_url, out_headers, out_params = self._apply(url, headers)
        return self._with_query(out_url, out_params), out_headers

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
