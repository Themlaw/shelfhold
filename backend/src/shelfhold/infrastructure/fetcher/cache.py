"""The Fetcher's HTTP cache (RFC 9111) and conditional GET (ENF-LEG-007).

An in-memory, bounded, shared cache. What keeps it from ever serving the wrong caller:

- the key holds the source (a plugin id) and the URL, and a Vary match is checked on top;
- a request that carries anything but plain content negotiation (Authorization, Cookie, an API
  key, any header this module does not know to be harmless) neither reads nor writes the cache:
  no authenticated answer ever sits in a shared cache;
- `no-store`, `private`, `Vary: *` and `Set-Cookie` answers are not stored;
- only an answer received at the first hop is stored, so the domain rules and the SSRF checks of
  every later hop are never skipped by a hit (the caller checks the first hop before asking).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Final

from shelfhold.sdk.fetcher import Response

# Request headers that cannot identify an account. Anything else bypasses the cache.
_HARMLESS: Final = frozenset(
    {"accept", "accept-language", "accept-encoding", "user-agent", "referer", "host"}
)
_STORABLE: Final = frozenset({200, 203})
_MAX_VARIANTS: Final = 4
_HEURISTIC_CAP_S: Final = 24 * 3600


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _directives(headers: Mapping[str, str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in headers.get("cache-control", "").split(","):
        name, _, value = part.strip().partition("=")
        if name:
            found[name.lower()] = value.strip().strip('"')
    return found


def _seconds(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


def _http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None


def cacheable_request(headers: Mapping[str, str]) -> bool:
    """False when the request may be tied to an account or asks not to be cached."""
    lowered = _lower(headers)
    if any(name not in _HARMLESS for name in lowered):  # a caller's own If-* too
        return False
    return "no-store" not in _directives(lowered)


@dataclass
class CacheEntry:
    status: int
    headers: dict[str, str]
    body: bytes
    stored_at: float
    fresh_for: float
    vary: dict[str, str]  # request header -> the value it had when this was stored

    @property
    def validators(self) -> dict[str, str]:
        found: dict[str, str] = {}
        if "etag" in self.headers:
            found["If-None-Match"] = self.headers["etag"]
        if "last-modified" in self.headers:
            found["If-Modified-Since"] = self.headers["last-modified"]
        return found

    def response(self, now: float) -> Response:
        headers = dict(self.headers)
        headers["age"] = str(max(0, int(now - self.stored_at)))
        return Response(status=self.status, headers=headers, body=self.body)


@dataclass
class HttpCache:
    max_entries: int = 256
    max_entry_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 32 * 1024 * 1024
    clock: Callable[[], float] = time.time
    _entries: OrderedDict[tuple[str, str], list[CacheEntry]] = field(default_factory=OrderedDict)
    _total: int = 0

    # -- reading ---------------------------------------------------------------------------

    def lookup(self, source: str, url: str, request: Mapping[str, str]) -> CacheEntry | None:
        key = (source, url)
        wanted = _lower(request)
        for entry in self._entries.get(key, ()):
            if all(wanted.get(n, "") == v for n, v in entry.vary.items()):
                self._entries.move_to_end(key)
                return entry
        return None

    def is_fresh(self, entry: CacheEntry) -> bool:
        return self.clock() - entry.stored_at < entry.fresh_for

    def serve(self, entry: CacheEntry) -> Response:
        return entry.response(self.clock())

    def revalidated(self, entry: CacheEntry, not_modified: Mapping[str, str]) -> Response:
        """A 304: the stored answer lives on, with the headers the 304 refreshed."""
        for name, value in _lower(not_modified).items():
            if not name.startswith("content-") and name not in {"transfer-encoding", "connection"}:
                entry.headers[name] = value
        entry.stored_at = self.clock()
        control = _directives(entry.headers)
        entry.fresh_for = (
            0.0 if "no-cache" in control else _freshness(entry.headers, entry.stored_at)
        )
        return entry.response(self.clock())

    # -- writing ---------------------------------------------------------------------------

    def store(self, source: str, url: str, request: Mapping[str, str], response: Response) -> None:
        headers = _lower(response.headers)
        if response.status not in _STORABLE or len(response.body) > self.max_entry_bytes:
            return
        control = _directives(headers)
        if "no-store" in control or "private" in control or "set-cookie" in headers:
            return
        vary_names = [n.strip().lower() for n in headers.get("vary", "").split(",") if n.strip()]
        if "*" in vary_names:
            return
        fresh_for = 0.0 if "no-cache" in control else _freshness(headers, self.clock())
        if fresh_for <= 0 and "etag" not in headers and "last-modified" not in headers:
            return  # nothing to reuse and nothing to revalidate with
        wanted = _lower(request)
        entry = CacheEntry(
            status=response.status,
            headers={k: v for k, v in headers.items() if k != "age"},
            body=response.body,
            stored_at=self.clock(),
            fresh_for=fresh_for,
            vary={n: wanted.get(n, "") for n in vary_names},
        )
        self._drop_variant(source, url, entry.vary)
        key = (source, url)
        variants = self._entries.setdefault(key, [])
        variants.insert(0, entry)
        self._total += len(entry.body)
        for old in variants[_MAX_VARIANTS:]:
            self._total -= len(old.body)
        del variants[_MAX_VARIANTS:]
        self._entries.move_to_end(key)
        self._trim()

    def invalidate(self, source: str, url: str) -> None:
        for entry in self._entries.pop((source, url), []):
            self._total -= len(entry.body)

    def _drop_variant(self, source: str, url: str, vary: Mapping[str, str]) -> None:
        variants = self._entries.get((source, url), [])
        for old in [e for e in variants if e.vary == dict(vary)]:
            variants.remove(old)
            self._total -= len(old.body)

    def _trim(self) -> None:
        while self._entries and (
            len(self._entries) > self.max_entries or self._total > self.max_total_bytes
        ):
            _, dropped = self._entries.popitem(last=False)
            self._total -= sum(len(e.body) for e in dropped)


def _freshness(headers: Mapping[str, str], now: float) -> float:
    """How many seconds the answer stays fresh: max-age, Expires, else a heuristic."""
    control = _directives(headers)
    age = _seconds(headers.get("age")) or 0
    lifetime = _seconds(control.get("s-maxage"))  # this is a shared cache: s-maxage wins
    if lifetime is None:
        lifetime = _seconds(control.get("max-age"))
    if lifetime is None:
        expires, date = _http_date(headers.get("expires")), _http_date(headers.get("date"))
        if headers.get("expires") is not None:
            lifetime = int(expires - (date or now)) if expires is not None else 0
    if lifetime is not None:
        return float(max(0, lifetime - age))
    modified, date = _http_date(headers.get("last-modified")), _http_date(headers.get("date"))
    if modified is not None:
        return max(0.0, min(((date or now) - modified) * 0.1, _HEURISTIC_CAP_S))
    return 0.0


def cache_for(source: str, enabled: bool, off_sources: Collection[str]) -> HttpCache | None:
    """The cache of one source's Fetcher, or None when the cache is off for it (config
    `fetcher_cache`, `fetcher_cache_off_sources`)."""
    if not enabled or source in off_sources:
        return None
    return HttpCache()
