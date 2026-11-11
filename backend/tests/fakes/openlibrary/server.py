"""A simulated OpenLibrary: the books API, the cover host and robots.txt, no network.

It plays the referee of the politeness rules: every request is recorded with its `User-Agent`, its
host, its path and the (fake) instant it arrived at, so that a test can show that the identifying
agent was sent, that the requests were spaced by the token bucket and that a cached answer did not
come back to the server. Synthetic data only.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx2
from PIL import Image

from tests.fakes_fetcher import FakeResolver

API_HOST = "openlibrary.org"
COVER_HOST = "covers.openlibrary.org"
PUBLIC = "93.184.216.34"
ISBN = "9780306406157"  # a valid ISBN-13 (synthetic)
ISBN_OTHER = "9780306406164"  # another valid ISBN-13 (synthetic)
OL_ID = "OL1000M"


def png(size: tuple[int, int] = (60, 90), colour: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def record(**extra: Any) -> dict[str, Any]:
    """What `api/books?jscmd=data` answers for one book (the shape, with invented values)."""
    base: dict[str, Any] = {
        "url": f"https://{API_HOST}/books/{OL_ID}/Synthetic_title",
        "key": f"/books/{OL_ID}",
        "title": "Synthetic title",
        "authors": [
            {"url": f"https://{API_HOST}/authors/OL1A/A", "name": "Ada Example"},
            {"url": f"https://{API_HOST}/authors/OL2A/B", "name": "Bo Sample"},
        ],
        "publishers": [{"name": "Example Press"}],
        "publish_date": "2001",
        "subjects": [{"name": "Fiction", "url": f"https://{API_HOST}/subjects/fiction"}],
        "identifiers": {"openlibrary": [OL_ID], "isbn_13": [ISBN]},
        "cover": {"large": f"https://{COVER_HOST}/b/id/1-L.jpg"},
    }
    return {**base, **extra}


@dataclass
class Seen:
    method: str
    host: str
    path: str
    query: str
    user_agent: str | None
    headers: dict[str, str]
    at: float


@dataclass
class FakeOpenLibrary:
    """`clock`: the fake instant stamped on each request (the bucket's clock, in the tests)."""

    clock: Callable[[], float] = lambda: 0.0
    books: dict[str, dict[str, Any]] = field(default_factory=dict)
    covers: dict[str, bytes] = field(default_factory=dict)  # isbn -> image bytes
    robots: dict[str, str] = field(default_factory=dict)  # host -> robots.txt ("" = none: 404)
    status: int = 200  # for the books API: 503 makes the provider "down"
    cache_control: str | None = None
    etag: str | None = None
    seen: list[Seen] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.books.setdefault(ISBN, record())
        self.covers.setdefault(ISBN, png())

    # -- the wire ---------------------------------------------------------------------------
    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    @property
    def resolver(self) -> FakeResolver:
        return FakeResolver({API_HOST: [PUBLIC], COVER_HOST: [PUBLIC]})

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        headers = dict(request.headers)
        self.seen.append(
            Seen(request.method, request.headers["host"], request.url.path,
                 request.url.query.decode(), headers.get("user-agent"), headers, self.clock())
        )  # fmt: skip
        host, path = request.headers["host"], request.url.path
        if path == "/robots.txt":
            text = self.robots.get(host, "")
            if not text:
                return httpx2.Response(404)
            return httpx2.Response(200, text=text)
        if host == API_HOST and path == "/api/books":
            return self._books(request)
        if host == COVER_HOST and path.startswith("/b/isbn/"):
            isbn = path.removeprefix("/b/isbn/").split("-")[0].removesuffix(".jpg")
            body = self.covers.get(isbn)
            return httpx2.Response(200, content=body) if body is not None else httpx2.Response(404)
        return httpx2.Response(404)

    def _books(self, request: httpx2.Request) -> httpx2.Response:
        if self.status != 200:
            return httpx2.Response(self.status)
        keys = parse_qs(request.url.query.decode()).get("bibkeys", [""])[0].split(",")
        found = {}
        for key in keys:
            isbn = key.removeprefix("ISBN:")
            if key.startswith("ISBN:") and isbn in self.books:
                found[key] = self.books[isbn]
            elif key.startswith("OLID:"):
                found.update({key: b for b in self.books.values() if b["key"].endswith(key[5:])})  # noqa: B035
        headers = {}
        if self.cache_control:
            headers["cache-control"] = self.cache_control
        if self.etag:
            if request.headers.get("if-none-match") == self.etag:
                return httpx2.Response(304, headers={"etag": self.etag, **headers})
            headers["etag"] = self.etag
        return httpx2.Response(200, content=json.dumps(found).encode(), headers=headers)

    # -- what a test asks ------------------------------------------------------------------
    def requests_to(self, host: str, path_prefix: str = "/") -> list[Seen]:
        return [s for s in self.seen if s.host == host and s.path.startswith(path_prefix)]

    def api_calls(self) -> list[Seen]:
        return self.requests_to(API_HOST, "/api/books")
