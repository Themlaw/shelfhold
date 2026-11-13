"""The real address of a caller, and rate limits on the public routes (ENF-SEC-008, D15).

- `real_client_ip`: the socket peer, unless the peer is a trusted proxy (`http.trusted_proxies`);
  then `X-Forwarded-For` is read **from the right**: the first address that is not itself a
  trusted proxy. What a client wrote to the left of that is a claim, never believed. Anything that
  is not an address (a name, a port, brackets) stops the walk at the proxy: the caller gets no
  fresh budget by writing garbage. An IPv4-mapped IPv6 address is the IPv4 address it wraps.
- `SlidingWindowLimiter`: a table of at most `max_keys` entries (the least recently seen goes
  first), each holding at most `limit` timestamps: its memory does not grow with the traffic.
- `PublicRateLimit`: requests of an anonymous caller, per real address.

State is in memory: one process per instance (`/data/.lock`); a restart forgets it.
"""

import hmac
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from shelfhold.identity.public import Principal
from shelfhold.identity.public import address_key as address_key
from shelfhold.interfaces.http_api.catalog import error_code
from shelfhold.interfaces.http_api.identity_headers import IpNetwork, normalize_ip
from shelfhold.interfaces.http_api.problems import build_problem_response

UNKNOWN = "unknown"
DEFAULT_MAX_KEYS = 10_000
# What no anonymous quota ever slows down: the probes of the orchestrator.
UNLIMITED_PATHS = frozenset({"/healthz", "/readyz"})


def _in(address: str, trusted: tuple[IpNetwork, ...]) -> bool:
    parsed = normalize_ip(address)
    return parsed is not None and any(parsed in network for network in trusted)


def real_client_ip(
    peer: str | None, forwarded: Sequence[str], trusted: tuple[IpNetwork, ...]
) -> str:
    """`forwarded`: the values of every `X-Forwarded-For` line, in the order received."""
    parsed = normalize_ip(peer) if peer else None
    if parsed is None:
        return UNKNOWN
    if not trusted or not any(parsed in network for network in trusted):
        return str(parsed)
    chain = [part.strip() for value in forwarded for part in value.split(",")]
    for entry in reversed(chain):
        address = normalize_ip(entry)
        if address is None:  # not an address: the walk stops at the proxy that wrote it
            return str(parsed)
        if not any(address in network for network in trusted):
            return str(address)
    return str(parsed)


class SlidingWindowLimiter:
    def __init__(self, *, limit: int, window_s: float, max_keys: int = DEFAULT_MAX_KEYS) -> None:
        self._limit = limit
        self._window = window_s
        self._max_keys = max_keys
        self._seen: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._seen)

    def _recent(self, key: str, now: float) -> deque[float] | None:
        stamps = self._seen.get(key)
        if stamps is None:
            return None
        while stamps and now - stamps[0] >= self._window:
            stamps.popleft()
        if not stamps:
            del self._seen[key]
            return None
        return stamps

    def _wait(self, key: str, now: float) -> float | None:
        stamps = self._recent(key, now)
        if stamps is not None and len(stamps) >= self._limit:
            return stamps[0] + self._window - now
        return None

    def _record(self, key: str, now: float) -> None:
        stamps = self._seen.setdefault(key, deque(maxlen=self._limit))
        stamps.append(now)
        self._seen.move_to_end(key)
        while len(self._seen) > self._max_keys:
            self._seen.popitem(last=False)

    def retry_after(self, key: str, *, now: float) -> float | None:
        with self._lock:
            return self._wait(key, now)

    def record(self, key: str, *, now: float) -> None:
        with self._lock:
            self._record(key, now)

    def hit(self, key: str, *, now: float) -> float | None:
        """Atomically: refuse (seconds to wait, the attempt is not counted) or count it."""
        with self._lock:
            wait = self._wait(key, now)
            if wait is None:
                self._record(key, now)
            return wait


# The routes with a credential of their own (device token in the path, or Basic): never counted
# as anonymous callers when they carry one (the routes themselves answer a wrong one).
DEVICE_PREFIXES = ("/opds", "/kosync", "/kobo")


def device_credential(scope: Scope) -> str | None:
    """A digest of the credential of a request to a device route, or None (no credential, or not
    a device route). `/kobo/<token>/…` carries it in the path; OPDS and KOSync in `Authorization`
    (or KOSync's `x-auth-key`)."""
    path = str(scope.get("path", ""))
    prefix = next((p for p in DEVICE_PREFIXES if path == p or path.startswith(p + "/")), None)
    if prefix is None:
        return None
    headers = Headers(scope=scope)
    raw = headers.get("authorization") or headers.get("x-auth-key") or ""
    if not raw and prefix == "/kobo":
        parts = path.split("/")
        raw = parts[2] if len(parts) > 2 else ""
    return credential_label(raw) if raw else None


_LABEL_KEY = secrets.token_bytes(32)  # per process: a label cannot be recomputed from a guess


def credential_label(raw: str) -> str:
    """A keyed digest of a credential, only ever compared in memory (a Basic carries a password:
    it is never kept, logged or hashed for storage, so no password-hashing cost applies)."""
    return "cred:" + hmac.new(_LABEL_KEY, raw.encode(), "sha256").hexdigest()[:24]


class RecognisedCredentials:
    """The device credentials a route has **accepted** (a token it issued, a Basic it checked).

    Only those buy a budget of their own: a credential nobody accepted is a guess, and a guess
    is counted against the address like any other anonymous request, so that changing it at each
    request never escapes the limit (review PR #232). The set is bounded (least recently seen
    goes first) and forgets on restart, which only sends a device back to the address budget
    until its next accepted request."""

    def __init__(self, max_keys: int = DEFAULT_MAX_KEYS) -> None:
        self._max = max_keys
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def add_raw(self, raw: str) -> None:
        label = credential_label(raw)
        with self._lock:
            self._seen[label] = None
            self._seen.move_to_end(label)
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)

    def __contains__(self, label: str) -> bool:
        with self._lock:
            return label in self._seen

    def __len__(self) -> int:
        return len(self._seen)


def recognise_device_credential(scope: Scope) -> None:
    """To call from the authentication of a device route once it has accepted the credential of
    this request (`/opds`, `/kosync`, `/kobo`)."""
    registry = scope.get("app")
    known = getattr(getattr(registry, "state", None), "recognised_devices", None)
    headers = Headers(scope=scope)
    raw = headers.get("authorization") or headers.get("x-auth-key") or ""
    if not raw:
        parts = str(scope.get("path", "")).split("/")
        raw = parts[2] if len(parts) > 2 and parts[1] == "kobo" else ""
    if isinstance(known, RecognisedCredentials) and raw:
        known.add_raw(raw)


class ClientIpMiddleware:
    """Puts the real address in `scope["state"]["client_ip"]` for everything below."""

    def __init__(self, app: ASGIApp, *, trusted: tuple[IpNetwork, ...] = ()) -> None:
        self.app = app
        self.trusted = trusted

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            client = scope.get("client")
            forwarded = Headers(scope=scope).getlist("x-forwarded-for") if self.trusted else []
            ip = real_client_ip(client[0] if client else None, forwarded, self.trusted)
            scope.setdefault("state", {})["client_ip"] = ip
        await self.app(scope, receive, send)


def client_ip_of(scope: Scope) -> str:
    value = scope.get("state", {}).get("client_ip")
    if isinstance(value, str):
        return value
    client = scope.get("client")
    parsed = normalize_ip(client[0]) if client else None
    return str(parsed) if parsed is not None else UNKNOWN


def retry_after_header(wait: float) -> int:
    return max(1, int(wait) + (0 if wait == int(wait) else 1))


async def refuse_too_many(scope: Scope, receive: Receive, send: Send, wait: float) -> None:
    seconds = retry_after_header(wait)
    response = build_problem_response(
        error_code("too_many_attempts"),
        request_id=scope.get("state", {}).get("request_id"),
        extensions={"retry_after_s": seconds},
        headers={"Retry-After": str(seconds)},
    )
    await response(scope, receive, send)


class PublicRateLimit:
    """Requests of a caller nobody signed in, per real address (ENF-SEC-008). Sits inside
    authentication: a principal, session or key, is never counted here."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        per_minute: int,
        per_credential: int = 600,
        recognised: RecognisedCredentials | None = None,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self._limiter = SlidingWindowLimiter(limit=per_minute, window_s=60.0, max_keys=max_keys)
        self._by_credential = SlidingWindowLimiter(
            limit=per_credential, window_s=60.0, max_keys=max_keys
        )
        # accepted credentials: their own budget, and a ceiling per address ten times higher
        self._by_device_address = SlidingWindowLimiter(
            limit=per_minute * 10, window_s=60.0, max_keys=max_keys
        )
        self._recognised = recognised if recognised is not None else RecognisedCredentials()
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in UNLIMITED_PATHS:
            await self.app(scope, receive, send)
            return
        if isinstance(scope.get("state", {}).get("principal"), Principal):
            await self.app(scope, receive, send)
            return
        label = device_credential(scope)
        now = self._clock()
        address = address_key(client_ip_of(scope))
        if label is not None and label in self._recognised:
            # accepted before: its own budget (a sync of a whole library is many requests),
            # and a higher ceiling for the address
            wait = self._by_credential.hit(label, now=now)
            if wait is None:
                wait = self._by_device_address.hit(address, now=now)
        else:  # no credential, or one nobody accepted: an anonymous caller
            wait = self._limiter.hit(address, now=now)
        if wait is not None:
            await refuse_too_many(scope, receive, send, wait)
            return
        await self.app(scope, receive, send)
