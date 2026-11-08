"""Secret injection by the Fetcher (EF-SRC-023): V1 implements Basic and ApiKey, and the JWT
pair of a multi-account service (`JwtFetcher`, EF-SRC-031).

A plugin never reads a secret: the host wraps the Fetcher it hands over, and the wrapper adds the
credential to each request. The secret is held in the wrapper and is never rendered.
"""

import asyncio
import base64
import json
import secrets
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, quote_plus, urlencode, urlsplit, urlunsplit

from sqlalchemy import Connection

from shelfhold.infrastructure.fetcher.errors import InvalidFetchUrl
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.secrets.envelope import Envelope, open_with, seal
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.plugin_host.errors import AuthExpired, UnsupportedAuthFlow
from shelfhold.sdk.fetcher import CookieJarView, DownloadedFile, Fetcher, Response
from shelfhold.sdk.models import AuthApiKey, AuthBasic, AuthFlow
from shelfhold.sdk.refs import ImageRequest
from shelfhold.sdk.source import SourceChanged, TemporarilyUnavailable

__all__ = [
    "AuthInjectingFetcher",
    "JwtDialect",
    "JwtFetcher",
    "UnsupportedAuthFlow",
    "load_account_secret",
]


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


# =============================================================================================
# The JWT pair of a multi-account service (EF-SRC-031). The token handling is gathered here so
# that it can be audited in one read: what holds a token, where one is registered for the log
# scrubber, where one is sent, where one is replaced. Nothing below logs anything.
# =============================================================================================


@dataclass(frozen=True)
class JwtDialect:
    """How one service speaks login and refresh: the documents it is sent and the answers it gives.
    The adapter of the service owns it (the host core names no service), the exact forms are the
    ones its contract test verified."""

    path: str  # the GraphQL endpoint, relative to the origin
    login: Callable[[str, str], str]  # (username, password) -> document
    refresh: Callable[[str], str]  # (refresh token) -> document
    login_field: str = "login"  # the root field of each answer
    refresh_field: str = "refreshToken"
    access_lifetime: float = 300.0  # seconds a fresh access token is good for
    renew_margin: float = 60.0  # renewed this long before it ends, not after a refusal


class _Pair:
    """An access token and its refresh token, immutable, never rendered. A rotation builds a
    whole new pair and swaps the reference once: nobody ever sees half of two pairs."""

    __slots__ = ("_access", "_refresh", "generation", "renew_at")

    def __init__(self, access: str, refresh: str | None, generation: int, renew_at: float) -> None:
        self._access, self._refresh = access, refresh
        self.generation, self.renew_at = generation, renew_at

    def __repr__(self) -> str:
        return f"<token pair #{self.generation} redacted>"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("a token pair is never serialised")

    @property
    def access(self) -> str:
        return self._access

    @property
    def refresh(self) -> str | None:
        return self._refresh


def _register_token(token: str) -> None:
    """Every form of a token that can reach a log line: bare, as the Bearer header value."""
    register_secret_value(token)
    register_secret_value(f"Bearer {token}")


def _literal_forms(value: str) -> list[str]:
    """The password as typed, as it sits inside a GraphQL string literal (escaped once), and as it
    sits in the JSON body that carries that document (escaped twice)."""
    once = json.dumps(value)[1:-1]
    return [value, once, json.dumps(once)[1:-1]]


class JwtFetcher:
    """Wraps a Fetcher: logs in with the service account, sends `Authorization: Bearer`, renews the
    access token before it ends or when the service refuses it, and only towards the origin.

    - The tokens live in memory only (`_pair`), never in the database or on disk.
    - A rotation replaces the pair whole, under one lock; a caller that was refused with an older
      pair finds the newer one in place and does not refresh again: one refresh per expiry.
    - The refresh token that is refused leads to one new login with the account; an account the
      service refuses raises `AuthExpired` and is not retried by this wrapper.
    """

    has_credentials = True
    flow_kind = "jwt"

    def __init__(
        self,
        inner: Fetcher,
        dialect: JwtDialect,
        secret: Mapping[str, str],
        *,
        origin: str,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        declared = _origin(origin)
        if declared is None:
            raise InvalidFetchUrl("the origin of an authenticated service must be http(s)://host")
        self._inner: Any = inner
        self._dialect = dialect
        self._origin = declared
        self._base = origin.rstrip("/")
        self._now = now
        self._pair: _Pair | None = None
        # One renewal at a time, whatever the number of event loops (threads) sharing this wrapper:
        # `_loop_locks` serialises the callers of one loop, `_renewing` those of all the loops.
        self._loop_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        self._map_guard = threading.Lock()
        self._renewing = threading.Lock()
        # the account, sealed under an ephemeral in-process key like the Basic wrapper does
        self._key = secrets.token_bytes(32)
        self._row = secrets.token_hex(8)
        self._sealed: Envelope = seal(
            self._key, "ephemeral", json.dumps(dict(secret)).encode(),
            table="authflow", column="secret", row_id=self._row,
        )  # fmt: skip
        for form in _literal_forms(secret["password"]):
            register_secret_value(form)

    def __repr__(self) -> str:
        return f"JwtFetcher(flow={self.flow_kind!r}, generation={self.generation})"

    @property
    def generation(self) -> int:
        """How many times the pair was replaced (a login or a refresh each count once)."""
        return self._pair.generation if self._pair else 0

    # -- the token endpoint ------------------------------------------------------------------
    def _account(self) -> dict[str, str]:
        plain = open_with(
            self._key, self._sealed, table="authflow", column="secret", row_id=self._row
        )
        loaded: dict[str, str] = json.loads(plain)
        return loaded

    async def _token_call(self, document: str, field: str) -> dict[str, Any] | None:
        """One login or refresh. The tokens it answers, or None when the service refuses them;
        a transport or protocol problem is raised, never turned into a refusal."""
        response = await self._inner.post(
            self._base + self._dialect.path,
            json={"query": document},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if response.status in (401, 403):
            return None
        if response.status == 429 or response.status >= 500:
            raise TemporarilyUnavailable(f"the token endpoint answered {response.status}")
        if response.status >= 400:
            raise SourceChanged(f"unexpected answer {response.status} from the token endpoint")
        try:
            payload = json.loads(response.body)
        except ValueError as exc:
            raise SourceChanged("the token endpoint did not answer with JSON") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        granted = data.get(field) if isinstance(data, dict) else None
        if not isinstance(granted, dict) or not isinstance(granted.get("accessToken"), str):
            return None  # an `errors` answer: the credentials were not accepted
        return granted

    def _new_pair(self, granted: dict[str, Any], previous: _Pair | None) -> _Pair:
        access = str(granted["accessToken"])
        refresh = granted.get("refreshToken")
        kept = str(refresh) if isinstance(refresh, str) and refresh else None
        if kept is None and previous is not None:
            kept = previous.refresh  # the service did not rotate it: the same one stays good
        for token in (access, kept):
            if token:
                _register_token(token)
        life = self._dialect.access_lifetime - self._dialect.renew_margin
        return _Pair(access, kept, self.generation + 1, self._now() + life)

    async def _renew(self, seen: int) -> _Pair:
        """Replace the pair, once per expiry. `seen`: the generation the caller was refused with."""
        async with self._lock_for_this_loop():
            # The thread lock is held across the network calls on purpose: a second thread (its
            # own event loop) waits here and, finding a newer pair, does not refresh again. It
            # blocks only a loop whose caller is refused with an out-of-date pair.
            with self._renewing:
                return await self._renew_locked(seen)

    async def _renew_locked(self, seen: int) -> _Pair:
        current = self._pair
        if current is not None and current.generation != seen:
            return current  # another caller already rotated it while this one waited
        granted = None
        if current is not None and current.refresh:
            document = self._dialect.refresh(current.refresh)
            granted = await self._token_call(document, self._dialect.refresh_field)
        if granted is None:  # no refresh token, or it is dead: the account logs in again
            account = self._account()
            document = self._dialect.login(account["username"], account["password"])
            granted = await self._token_call(document, self._dialect.login_field)
        if granted is None:
            raise AuthExpired("the service refused the account")
        self._pair = self._new_pair(granted, current)  # the one place a pair is replaced
        return self._pair

    def _lock_for_this_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._map_guard:  # an asyncio lock belongs to the loop that uses it
            lock = self._loop_locks.get(loop)
            if lock is None:
                lock = self._loop_locks[loop] = asyncio.Lock()
            return lock

    async def _fresh(self) -> _Pair:
        pair = self._pair
        if pair is None or self._now() >= pair.renew_at:
            return await self._renew(pair.generation if pair else 0)
        return pair

    # -- what is injected --------------------------------------------------------------------
    def _check_origin(self, url: str) -> None:
        if _origin(url) != self._origin:
            raise InvalidFetchUrl("the credential is only sent to the declared service origin")

    @staticmethod
    def _bearer(pair: _Pair, headers: Mapping[str, str] | None) -> dict[str, str]:
        out = dict(headers or {})
        out["Authorization"] = "Bearer " + pair.access
        return out

    @staticmethod
    def _refused(response: Response) -> bool:
        """The service says the token is no good: 401 (REST), or a 200 GraphQL `Unauthorized`.
        A CSRF 401 is not that (it is a version problem and asks for no credentials)."""
        if response.status == 401:
            return b"csrf" not in response.body.lower()
        if response.status != 200 or b"nauthorized" not in response.body:
            return False
        try:
            payload = json.loads(response.body)
        except ValueError:
            return False
        errors = payload.get("errors") if isinstance(payload, dict) else None
        return isinstance(errors, list) and any(
            isinstance(e, dict) and str(e.get("message", "")).strip().casefold() == "unauthorized"
            for e in errors
        )

    async def _send(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        call: Callable[[dict[str, str]], Awaitable[Response]],
    ) -> Response:
        self._check_origin(url)
        pair = await self._fresh()
        response = await call(self._bearer(pair, headers))
        if self._refused(response):  # the token ended earlier than expected: renew, try once more
            pair = await self._renew(pair.generation)
            response = await call(self._bearer(pair, headers))
        return response

    def authorize(
        self, url: str, headers: Mapping[str, str] | None = None
    ) -> tuple[str, dict[str, str]]:
        """The URL and headers of a streamed download (the bounded receiver). Synchronous, so it
        cannot renew: it uses the pair in place, which the requests that precede a download have
        just renewed."""
        self._check_origin(url)
        if self._pair is None:
            raise AuthExpired("the service account is not logged in")
        return url, self._bearer(self._pair, headers)

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
        extra: dict[str, Any] = {"referer": referer} if referer else {}
        if cache_ttl is not None:
            extra["cache_ttl"] = cache_ttl

        async def call(hdrs: dict[str, str]) -> Response:
            response: Response = await self._inner.get(url, headers=hdrs, params=params, **extra)
            return response

        return await self._send(url, headers, call)

    async def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        async def call(hdrs: dict[str, str]) -> Response:
            response: Response = await self._inner.post(url, data=data, json=json, headers=hdrs)
            return response

        return await self._send(url, headers, call)

    async def download(self, req: ImageRequest) -> DownloadedFile:
        self._check_origin(req.url)
        pair = await self._fresh()
        injected = req.model_copy(update={"headers": self._bearer(pair, req.headers)})
        downloaded: DownloadedFile = await self._inner.download(injected)
        return downloaded

    def cookies(self) -> CookieJarView:
        view: CookieJarView = self._inner.cookies()
        return view
