# SPDX-License-Identifier: MIT
"""The Google Books provider: an ISBN or a title in, proposals out (EF-BIB-013, ENF-LEG-005).

The key is the instance's own (never one of the project's, shared by everyone): Google publishes no
quota figure, the real one is that of the owner's cloud console, so the host holds a cautious daily
budget (1000 by default, adjustable) counted per Pacific day, the day Google's quotas reset in. A
429 that says «Queries per day» is told apart from one per minute: this provider raises
`daily_quota`, and the host suspends it until Pacific midnight and falls back on the others.

The key travels in the `x-goog-api-key` header, never in an address, so the Fetcher (which logs the
route only, and never files a request that carries a credential) is the only one to see it; this
module never puts it in a message, an error, a repr or a result. What a person typed is one query
parameter, handed over as data. What comes back is believed for its shape only.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.metadata import (
    EntityRef,
    FieldProposal,
    IdentifyResult,
    MatchHints,
    MetadataManifest,
    Provenance,
    ProviderCapability,
    ProviderQuota,
    ProviderUnavailable,
)
from shelfhold.sdk.models import API_VERSION, AuthFlow, DomainRule, Permissions, SessionState
from shelfhold.sdk.refs import ImageRequest

KEY = "googlebooks"
API_HOST = "www.googleapis.com"
API_URL = f"https://{API_HOST}/books/v1/volumes"
SITE = "https://books.google.com"
TERMS_URL = "https://developers.google.com/books/terms"
ATTRIBUTION = "Data from Google Books (books.google.com)"
LICENCE = "Google Books API"
PACIFIC = "America/Los_Angeles"  # the zone whose midnight resets Google's quotas
DEFAULT_DAILY_BUDGET = 1000  # the lowest figure reported; the real one is the owner's console
MAX_DAILY_BUDGET = 1_000_000
REQUESTS_PER_SECOND = 1.0  # well under the per-minute limits; the day is what matters
MAX_QUERY = 200
MAX_TEXT = 2000
CONFIDENCE = 0.8
_VOLUME_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_INFO_LINK = re.compile(
    r"^https://books\.google\.com/books\?id=([A-Za-z0-9_-]{1,40})(?:&[A-Za-z0-9_=%.-]{0,100})?$"
)
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_DATE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")


def build_manifest(daily_budget: int = DEFAULT_DAILY_BUDGET) -> MetadataManifest:
    if not 1 <= daily_budget <= MAX_DAILY_BUDGET:
        raise ValueError("the daily budget of Google Books is a positive number of requests")
    return MetadataManifest(
        key=KEY,
        version="0.1.0",
        api=API_VERSION,
        name="Google Books",
        permissions=Permissions(domains=[DomainRule(pattern=API_HOST, purpose="api")]),
        terms_url=TERMS_URL,
        respects_robots=True,
        attribution=ATTRIBUTION,
        homepage=SITE,
        capabilities=ProviderCapability.IDENTIFY,  # no cover (hotlinked images), no account
        touched_fields={"title", "description", "publisher", "published_on"},
        entity_types={"series", "work", "edition", "volume"},
        langs=[],
        quota=ProviderQuota(
            requests_per_second=REQUESTS_PER_SECOND,
            burst=1,
            daily_cap=daily_budget,
            day_timezone=PACIFIC,
        ),
        settings_schema={
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                    "title": "Google Books API key (the instance's own)",
                }
            },
        },
    )


class GoogleBooksProvider:
    def __init__(
        self, *, api_key: str | SecretStr | None = None, daily_budget: int = DEFAULT_DAILY_BUDGET
    ) -> None:
        self.manifest = build_manifest(daily_budget)
        text = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        text = (text or "").strip()
        self._api_key = SecretStr(text) if text else None  # a SecretStr: no repr gives it away

    def __repr__(self) -> str:
        return f"GoogleBooksProvider(api_key={'set' if self._api_key else 'unset'})"

    async def identify(self, f: Fetcher, target: EntityRef, hints: MatchHints) -> IdentifyResult:
        if self._api_key is None:
            raise ProviderUnavailable("unauthorized")  # nothing is asked without the key
        query = self._query(target, hints)
        if query is None:
            return IdentifyResult()
        response = await f.get(
            API_URL,
            params={"q": query, "maxResults": "5", "printType": "books"},
            headers={"x-goog-api-key": self._api_key.get_secret_value()},
        )
        _check_status(response.status, response.body)
        try:
            data = json.loads(response.body)
        except (ValueError, TypeError):
            raise ProviderUnavailable("schema_mismatch") from None
        if not isinstance(data, dict):
            raise ProviderUnavailable("schema_mismatch")
        items = data.get("items")
        if items is None:
            return IdentifyResult()  # nothing known: no guess
        if not isinstance(items, list):
            raise ProviderUnavailable("schema_mismatch")
        if not items:
            return IdentifyResult()
        first = items[0]
        if not isinstance(first, dict):
            raise ProviderUnavailable("schema_mismatch")
        return self._proposals(target, first)

    @staticmethod
    def _query(target: EntityRef, hints: MatchHints | None) -> str | None:
        for scheme in ("isbn13", "isbn10"):
            value = target.ids.get(scheme)
            if value and value.isalnum() and len(value) in (10, 13):
                return f"isbn:{value}"
        title = _text(hints.title if hints is not None else None, MAX_QUERY)
        return f"intitle:{title}" if title else None

    def _proposals(self, target: EntityRef, item: dict[str, Any]) -> IdentifyResult:
        info = item.get("volumeInfo")
        if not isinstance(info, dict):
            raise ProviderUnavailable("schema_mismatch")
        link = info.get("infoLink")
        page = link if isinstance(link, str) and _INFO_LINK.match(link) else None
        if page is None and isinstance(item.get("id"), str) and _VOLUME_ID.match(item["id"]):
            page = f"{SITE}/books?id={item['id']}"
        provenance = Provenance(
            provider=KEY,
            fetched_at=datetime.now(UTC),
            source_url=page or SITE,
            licence=LICENCE,
            attribution=ATTRIBUTION,
        )

        def propose(field: str, value: str) -> FieldProposal:
            return FieldProposal(
                entity_type=target.entity_type,
                field=field,
                value=value,
                confidence=CONFIDENCE,
                provenance=provenance,
            )

        found: list[FieldProposal] = []
        if title := _text(info.get("title"), MAX_QUERY):
            found.append(propose("title", title))
        if description := _text(info.get("description"), MAX_TEXT):
            found.append(propose("description", description))
        if publisher := _text(info.get("publisher"), MAX_QUERY):
            found.append(propose("publisher", publisher))
        published = info.get("publishedDate")
        if (
            isinstance(published, str)
            and _DATE.match(published)
            and 1800 <= int(published[:4]) <= 2200
        ):
            found.append(propose("published_on", published))
        return IdentifyResult(proposals=found)

    async def get_cover(self, f: Fetcher, target: EntityRef) -> ImageRequest | None:
        return None

    async def login(self, f: Fetcher, flow: AuthFlow, creds: dict[str, Any]) -> SessionState:
        raise NotImplementedError("Google Books uses the instance's own API key")

    async def refresh(self, f: Fetcher, s: SessionState) -> SessionState:
        raise NotImplementedError("Google Books uses the instance's own API key")

    def url_for(self, ids: dict[str, str]) -> str | None:
        value = ids.get(KEY)
        return f"{SITE}/books?id={value}" if value and _VOLUME_ID.match(value) else None

    def ids_from_url(self, url: str) -> dict[str, str] | None:
        found = _INFO_LINK.match(url)
        return {KEY: found.group(1)} if found else None


def _check_status(status: int, body: bytes | str) -> None:
    if status == 200:
        return
    if status == 429:
        raise ProviderUnavailable("daily_quota" if _is_per_day(body) else "rate_limited")
    if status in (400, 401, 403):  # an invalid key is a 400 at Google
        raise ProviderUnavailable("unauthorized")
    if status >= 500:
        raise ProviderUnavailable("http_503")
    raise ProviderUnavailable("schema_mismatch")


def _is_per_day(body: bytes | str) -> bool:
    """A 429 whose message names the quota «Queries per day». The words are read, not repeated."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    try:
        data = json.loads(text)
    except ValueError:
        return False
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return isinstance(message, str) and "queries per day" in message.lower()


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(_CONTROLS.sub("", value).split())[:limit].strip()
    return cleaned or None
