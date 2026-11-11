"""GuardedFetcher: the one outbound HTTP path (ENF-SEC-001).

Per hop: validate the URL, resolve the host once, validate every answer, then connect to the
validated address itself (the URL is rewritten to the IP; `Host` and the TLS SNI keep the name, so
there is no second resolution to rebind). Redirects are followed by hand and each one goes through
the same steps; credentials never follow a redirect to another origin. No proxy is ever used.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx2
import structlog

from shelfhold import __version__
from shelfhold.infrastructure.fetcher.errors import (
    DnsBlocked,
    DnsResolutionError,
    FetchError,
    FetchTimeout,
    FetchTooLarge,
    FetchUnreachable,
    InvalidFetchUrl,
    SsrfBlocked,
    TooManyRedirects,
    UnsafeRedirect,
)
from shelfhold.infrastructure.fetcher.guard import (
    DomainAllowlist,
    IPAddress,
    block_reason,
    is_sinkhole,
    lan_block_reason,
    parse_host,
)
from shelfhold.infrastructure.fetcher.lan import LanTargets
from shelfhold.infrastructure.fetcher.resolver import Resolver
from shelfhold.infrastructure.logging import elapsed_ms, known_secret_values, route_of
from shelfhold.sdk.fetcher import Response

_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})
# Off origin, only these request headers travel: an allow-list cannot miss a new credential name.
_CROSS_ORIGIN_HEADERS: Final = frozenset(
    {"accept", "accept-language", "accept-encoding", "user-agent"}
)
_DEFAULT_PORT: Final = {"http": 80, "https": 443}


@dataclass(frozen=True)
class FetchLimits:
    max_bytes: int = 32 * 1024 * 1024  # decoded body
    total_timeout: float = 60.0  # whole call: every hop, every byte
    connect_timeout: float = 10.0
    read_timeout: float = 30.0  # between two chunks
    max_redirects: int = 5


class Sink(Protocol):
    """Where a download goes: a file object opened for writing, never the whole body in memory."""

    def write(self, data: bytes, /) -> object: ...


@dataclass(frozen=True)
class Download:
    status: int
    headers: dict[str, str]
    size: int  # bytes written to the sink (0 for a non-2xx status: nothing is written)
    sha256: str  # `sha256:<hex>` of those bytes


class _Tally:
    """Counts and hashes what goes to the sink."""

    def __init__(self, sink: Sink) -> None:
        self.sink = sink
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, chunk: bytes) -> None:
        self.sink.write(chunk)
        self.size += len(chunk)
        self.digest.update(chunk)


log = structlog.get_logger(__name__)


class GuardedFetcher:
    def __init__(
        self,
        resolver: Resolver,
        *,
        limits: FetchLimits | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        tls_context: ssl.SSLContext | None = None,
        user_agent: str = f"Shelfhold/{__version__}",
        domains: DomainAllowlist | None = None,
    ) -> None:
        self._resolver = resolver
        self._domains = domains  # a plugin's Fetcher: its DomainRules, checked at every hop
        self._lan = LanTargets.empty()  # the default fetcher, used for upstream URLs, has none
        self._limits = limits or FetchLimits()
        self._user_agent = user_agent
        if tls_context is not None and (
            tls_context.verify_mode == ssl.CERT_NONE or not tls_context.check_hostname
        ):
            raise ValueError("tls_context must verify certificates and host names")
        # No keep-alive: a pooled connection is keyed by address, and two names that share an
        # address must never share a TLS session validated for only one of them.
        transport = transport or httpx2.AsyncHTTPTransport(
            verify=tls_context if tls_context is not None else True,
            limits=httpx2.Limits(max_keepalive_connections=0),
            trust_env=False,
        )
        timeout = httpx2.Timeout(self._limits.read_timeout, connect=self._limits.connect_timeout)
        self._client = httpx2.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def secret_values(self) -> frozenset[str]:
        """The values this Fetcher may put in a request (and every other registered secret), with
        the forms a URL carries them in: what a reference is checked against (EF-SRC-035)."""
        return known_secret_values()

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        referer: str | None = None,
        max_bytes: int | None = None,
    ) -> Response:
        merged = dict(headers or {})
        if referer:
            merged["Referer"] = referer
        return await self._fetch("GET", url, merged, params, {}, max_bytes, None, None)

    async def post(
        self,
        url: str,
        *,
        data: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> Response:
        body: dict[str, Any] = {}
        if data is not None:
            body["data"] = dict(data)
        if json is not None:
            body["json"] = json
        return await self._fetch(
            "POST", url, dict(headers or {}), None, body, max_bytes, None, None
        )

    async def delete(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> Response:
        return await self._fetch(
            "DELETE", url, dict(headers or {}), params, {}, max_bytes, None, None
        )

    async def download(
        self,
        url: str,
        sink: Sink,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
        referer: str | None = None,
        total_timeout: float | None = None,
    ) -> Download:
        """GET a large body straight into `sink`, chunk by chunk.

        The bound is the call's own (`max_bytes`, no default: the caller states it), checked on the
        declared length first and then on the bytes actually received. Everything else is the
        `get` path: same URL checks, same pinned address, same redirections (the body of a
        redirection is never written). A non-2xx answer writes nothing and is returned as is.
        `total_timeout` replaces the whole-call limit for a download that legitimately takes long.
        """
        merged = dict(headers or {})
        if referer:
            merged["Referer"] = referer
        tally = _Tally(sink)
        response = await self._fetch("GET", url, merged, None, {}, max_bytes, tally, total_timeout)
        return Download(
            response.status, response.headers, tally.size, "sha256:" + tally.digest.hexdigest()
        )

    # -- internals -----------------------------------------------------------------------------

    async def _fetch(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        params: Mapping[str, Any] | None,
        body: dict[str, Any],
        max_bytes: int | None,
        tally: _Tally | None,
        total_timeout: float | None,
    ) -> Response:
        started = time.perf_counter()
        route = route_of(url)  # host and path: the query string may carry a key, never logged
        try:
            async with asyncio.timeout(
                self._limits.total_timeout if total_timeout is None else total_timeout
            ):
                response = await self._follow(method, url, headers, params, body, max_bytes, tally)
        except (TimeoutError, httpx2.TimeoutException) as exc:
            log.warning("http.failed", method=method, route=route, reason="timeout",
                        duration_ms=elapsed_ms(started))  # fmt: skip
            raise FetchTimeout("the response took too long") from exc
        except (httpx2.HTTPError, httpx2.StreamError) as exc:
            log.warning("http.failed", method=method, route=route, reason=type(exc).__name__,
                        duration_ms=elapsed_ms(started))  # fmt: skip
            raise FetchUnreachable(f"the request failed: {type(exc).__name__}") from exc
        except FetchError as exc:
            log.warning("http.refused", method=method, route=route, reason=type(exc).__name__,
                        duration_ms=elapsed_ms(started))  # fmt: skip
            raise
        log.debug("http.call", method=method, route=route, status=response.status,
                  duration_ms=elapsed_ms(started))  # fmt: skip
        return response

    async def _follow(
        self,
        method: str,
        raw_url: str,
        headers: dict[str, str],
        params: Mapping[str, Any] | None,
        body: dict[str, Any],
        max_bytes: int | None,
        tally: _Tally | None,
    ) -> Response:
        url = _parse_url(raw_url)
        if params:
            url = url.copy_merge_params(params)
        cap = self._limits.max_bytes if max_bytes is None else max_bytes
        first = _origin(url)
        for _ in range(self._limits.max_redirects + 1):
            if self._domains is not None:  # by name, before any resolution (EF-SRC-003)
                self._domains.check(url.raw_host.decode("ascii"), route_of(str(url)))
            # a LAN target is reachable only as the origin the caller asked for, never by redirect
            on_lan = _origin(url) == first and self._lan.addresses(url.host, _port(url)) is not None
            addresses = await self._pin(url, allow_lan=on_lan)
            response = await self._send(method, url, addresses, headers, body, cap, tally)
            location = response.headers.get("location")
            if response.status not in _REDIRECTS or not location:
                return response
            target = _parse_url(str(url.join(location)))
            if _origin(target) != _origin(url):
                if on_lan:
                    raise SsrfBlocked("a LAN target may not redirect to another origin")
                if response.status in (307, 308):
                    raise UnsafeRedirect("a 307/308 redirection to another origin is refused")
                headers = {k: v for k, v in headers.items() if k.lower() in _CROSS_ORIGIN_HEADERS}
            if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                method, body = "GET", {}
            url = target
        raise TooManyRedirects(f"more than {self._limits.max_redirects} redirections")

    async def _pin(self, url: httpx2.URL, *, allow_lan: bool) -> list[IPAddress]:
        """Resolve once and return the only addresses that may be connected to."""
        return await self.check_destination(url.host, _port(url), allow_lan=allow_lan)

    async def check_destination(
        self, host: str, port: int, *, allow_lan: bool = True
    ) -> list[IPAddress]:
        """The same verdict as a first hop, for a client that connects by itself (Apprise).

        It cannot pin the answer it validated, so the check is a gate at configuration and at
        send time, not a guarantee against a rebinding between the two (documented limit).
        """
        literal = parse_host(host)
        if literal is not None:
            answers = [literal]
        else:
            try:
                answers = [
                    ipaddress.ip_address(a) for a in await self._resolver.resolve(host, port)
                ]
            except ValueError as exc:
                raise DnsResolutionError(f"the resolver returned a non-address for {host}") from exc
        if not answers:
            raise DnsResolutionError(f"{host} does not resolve")
        if literal is None and all(is_sinkhole(a) for a in answers):
            raise DnsBlocked(f"{host} is blocked by the DNS resolver")
        declared = self._lan.addresses(host, port) if allow_lan else None
        if declared is not None:
            usable = [a for a in answers if a in declared and lan_block_reason(a) is None]
            if not usable:
                raise SsrfBlocked(f"{host}:{port} no longer answers with its declared addresses")
            return usable
        for address in answers:
            reason = block_reason(address)
            if reason is not None:
                raise SsrfBlocked(f"{host} -> {address} is refused: {reason}")
        return answers

    async def _send(
        self,
        method: str,
        url: httpx2.URL,
        addresses: list[IPAddress],
        headers: dict[str, str],
        body: dict[str, Any],
        cap: int,
        tally: _Tally | None,
    ) -> Response:
        host = url.host
        # the caller cannot choose the Host: it must name the host that was validated
        wire = {k: v for k, v in headers.items() if k.lower() != "host"}
        if not any(k.lower() == "user-agent" for k in wire):
            wire["User-Agent"] = self._user_agent
        wire["Host"] = _authority(url)
        extensions: dict[str, Any] = {}
        if url.scheme == "https" and parse_host(host) is None:
            extensions["sni_hostname"] = host
        last: Exception | None = None
        for address in addresses:
            request = self._client.build_request(
                method,
                url.copy_with(host=str(address)),
                headers=wire,
                extensions=extensions,
                **body,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx2.ConnectError as exc:
                last = exc
                continue
            except httpx2.TimeoutException:
                raise
            except httpx2.HTTPError as exc:
                raise FetchUnreachable(f"request to {host} failed: {type(exc).__name__}") from exc
            try:
                return await _read(response, cap, tally)
            except (httpx2.TimeoutException, FetchError):
                raise
            except (httpx2.HTTPError, httpx2.StreamError) as exc:
                raise FetchUnreachable(f"reading from {host} failed: {type(exc).__name__}") from exc
        raise FetchUnreachable(f"cannot connect to {host}") from last


async def _read(response: httpx2.Response, cap: int, tally: _Tally | None = None) -> Response:
    try:
        declared = response.headers.get("content-length")
        if (
            declared
            and declared.isdigit()
            and int(declared) > cap
            and not response.headers.get("content-encoding")
        ):
            raise FetchTooLarge(f"the response declares {declared} bytes, above {cap}")
        if tally is not None:
            # Only a 2xx body is a download. The body of a redirection or of an error is dropped
            # unread; the caller sees the status.
            if not 200 <= response.status_code < 300:
                return Response(status=response.status_code, headers=dict(response.headers))
            return await _stream(response, cap, tally)
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > cap:
                raise FetchTooLarge(f"the response is larger than {cap} bytes")
            chunks.append(chunk)
        return Response(
            status=response.status_code, headers=dict(response.headers), body=b"".join(chunks)
        )
    finally:
        await response.aclose()


async def _stream(response: httpx2.Response, cap: int, tally: _Tally) -> Response:
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > cap:
            raise FetchTooLarge(f"the response is larger than {cap} bytes")
        tally.write(chunk)
    return Response(status=response.status_code, headers=dict(response.headers))


def _parse_url(raw: str) -> httpx2.URL:
    try:
        url = httpx2.URL(raw)
    except httpx2.InvalidURL as exc:
        raise InvalidFetchUrl(f"invalid URL: {exc}") from exc
    if url.scheme not in _DEFAULT_PORT or not url.host:
        raise InvalidFetchUrl("only absolute http and https URLs are fetched")
    if url.userinfo:
        raise InvalidFetchUrl("credentials in a URL are refused; use an AuthFlow")
    if url.port is not None and not 0 < url.port < 65536:  # `or` would read 0 as "no port"
        raise InvalidFetchUrl("the port is out of range")
    return url


def _port(url: httpx2.URL) -> int:
    return url.port or _DEFAULT_PORT[url.scheme]


def _authority(url: httpx2.URL) -> str:
    name = f"[{url.host}]" if ":" in url.host else url.host
    return name if _port(url) == _DEFAULT_PORT[url.scheme] else f"{name}:{_port(url)}"


def _origin(url: httpx2.URL) -> tuple[str, str, int]:
    return url.scheme, url.host.lower(), _port(url)


class IntegrationFetcher(GuardedFetcher):
    """The Fetcher of a configured service (Kavita, Suwayomi): the only kind with `lan_targets`.

    A target is reachable only as the first hop of a call and only on its exact host and port; a
    redirection never enters one and never leaves one.
    """

    def __init__(
        self,
        resolver: Resolver,
        *,
        lan_targets: LanTargets,
        limits: FetchLimits | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        tls_context: ssl.SSLContext | None = None,
        user_agent: str = f"Shelfhold/{__version__}",
    ) -> None:
        super().__init__(
            resolver, limits=limits, transport=transport, tls_context=tls_context,
            user_agent=user_agent,
        )  # fmt: skip
        self._lan = lan_targets
