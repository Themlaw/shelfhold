# SPDX-License-Identifier: MIT
"""The ComicVine provider: a title in, proposals out (EF-BIB-013, ENF-LEG-005).

ComicVine is optional and off by default. Its terms are strict: non-commercial use only, a link
back to the site, 200 requests an hour per resource, and one API key per site, which here is the
instance's own (`SHELFHOLD_COMICVINE_API_KEY`), never one of the project. The host decides whether
the source is on, spends the hourly quota (`ProviderQuota`) and keeps every locked or hand-typed
value; this module only asks and reads.

Three cautions keep it honest. What a person typed is one query parameter, handed to the Fetcher
as data and never glued into an address. The key travels as a parameter too, so that the Fetcher
(which logs the route only, and never files a request that carries a credential) is the only one
to see it: this module never puts it in a message, an error, a repr or a result. And what comes
back is believed for its shape only: a link that is not a volume page of the site is not kept.

The API is JSON over HTTPS; nothing is written here.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr
from shelfhold_sdk.fetcher import Fetcher
from shelfhold_sdk.metadata import (
    EntityRef,
    FieldProposal,
    IdentifyResult,
    MatchHints,
    MetadataManifest,
    Provenance,
    ProviderCapability,
    ProviderQuota,
    ProviderUnavailable,
    RelationProposal,
)
from shelfhold_sdk.models import API_VERSION, AuthFlow, DomainRule, Permissions, SessionState
from shelfhold_sdk.refs import ImageRequest

KEY = "comicvine"
API_HOST = "comicvine.gamespot.com"
SITE = "https://comicvine.gamespot.com"
TERMS_URL = "https://comicvine.gamespot.com/api/documentation"
ATTRIBUTION = (
    "Data from ComicVine (comicvine.gamespot.com), non-commercial use only; "
    "a link back to ComicVine is required."
)
NOTICE = (
    "ComicVine is a non-commercial source: use it for your own library only, keep the link back "
    f"to the site that is shown with its data, and give your own API key. Terms: {TERMS_URL}"
)
LICENCE = "ComicVine API (non-commercial)"
REQUESTS_PER_HOUR = 200
MAX_TITLE = 200  # characters of what a person typed that go into the query
MAX_TEXT = 2000
CONFIDENCE = 0.8
CONFIDENCE_LINK = 0.6
_VOLUME = re.compile(r"^(?:[a-z0-9-]{0,200}/)?(4050-\d{1,12})/?$")
_SITE_PAGE = re.compile(rf"^{re.escape(SITE)}/((?:[a-z0-9-]{{0,200}}/)?4050-\d{{1,12}}/?)$")
_DIGITS = re.compile(r"^\d{1,12}$")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def build_manifest() -> MetadataManifest:
    return MetadataManifest(
        key=KEY,
        version="0.1.0",
        api=API_VERSION,
        name="ComicVine",
        permissions=Permissions(domains=[DomainRule(pattern=API_HOST, purpose="api")]),
        terms_url=TERMS_URL,
        respects_robots=True,
        attribution=ATTRIBUTION,
        homepage=SITE,
        capabilities=ProviderCapability.IDENTIFY,  # no cover, no account
        touched_fields={"title", "description", "publisher", "published_on"},
        entity_types={"series", "work", "edition", "volume"},
        langs=[],
        # 200 an hour, one every 18 seconds, no burst: the hour never holds more than 201.
        quota=ProviderQuota(requests_per_second=REQUESTS_PER_HOUR / 3600, burst=1),
        settings_schema={
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                    "title": "ComicVine API key (non-commercial, one per instance)",
                }
            },
        },
    )


class ComicVineProvider:
    def __init__(self, *, api_key: str | SecretStr | None = None) -> None:
        self.manifest = build_manifest()
        text = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        text = (text or "").strip()
        self._api_key = SecretStr(text) if text else None  # a SecretStr: no repr gives it away

    def __repr__(self) -> str:
        return f"ComicVineProvider(api_key={'set' if self._api_key else 'unset'})"

    async def identify(self, f: Fetcher, target: EntityRef, hints: MatchHints) -> IdentifyResult:
        if self._api_key is None:
            raise ProviderUnavailable("unauthorized")  # nothing is asked without the key
        title = _text(hints.title if hints is not None else None, MAX_TITLE)
        if title is None:
            return IdentifyResult()  # ComicVine is asked by title only
        response = await f.get(
            f"{SITE}/api/search/",
            params={
                "api_key": self._api_key.get_secret_value(),
                "format": "json",
                "limit": "5",
                "query": title,
                "resources": "volume",
            },
        )
        _check_status(response.status)
        try:
            data = json.loads(response.body)
        except (ValueError, TypeError):
            raise ProviderUnavailable("schema_mismatch") from None
        if not isinstance(data, dict):
            raise ProviderUnavailable("schema_mismatch")
        _check_body_status(data.get("status_code"))
        results = data.get("results")
        if not isinstance(results, list):
            raise ProviderUnavailable("schema_mismatch")
        if not results:
            return IdentifyResult()  # nothing known: no guess, not even a link
        first = results[0]
        if not isinstance(first, dict):
            raise ProviderUnavailable("schema_mismatch")
        return self._proposals(target, first)

    def _proposals(self, target: EntityRef, item: dict[str, Any]) -> IdentifyResult:
        page = _volume_page(item)
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
        if name := _text(item.get("name"), MAX_TITLE):
            found.append(propose("title", name))
        if deck := _text(item.get("deck"), MAX_TEXT):
            found.append(propose("description", deck))
        publisher = item.get("publisher")
        if isinstance(publisher, dict) and (name := _text(publisher.get("name"), MAX_TITLE)):
            found.append(propose("publisher", name))
        if year := _year(item.get("start_year")):
            found.append(propose("published_on", year))
        relations = []
        if page is not None:  # the link back: the volume's own page on the site
            relations.append(
                RelationProposal(
                    kind="official_link",
                    subject=target,
                    data={"url": page},
                    confidence=CONFIDENCE_LINK,
                    provenance=provenance,
                )
            )
        return IdentifyResult(proposals=found, relations=relations)

    async def get_cover(self, f: Fetcher, target: EntityRef) -> ImageRequest | None:
        return None

    async def login(self, f: Fetcher, flow: AuthFlow, creds: dict[str, Any]) -> SessionState:
        raise NotImplementedError("ComicVine uses the instance's own API key")

    async def refresh(self, f: Fetcher, s: SessionState) -> SessionState:
        raise NotImplementedError("ComicVine uses the instance's own API key")

    def url_for(self, ids: dict[str, str]) -> str | None:
        value = ids.get(KEY)
        return f"{SITE}/volume/{value}/" if value and _VOLUME.match(value) else None

    def ids_from_url(self, url: str) -> dict[str, str] | None:
        found = _SITE_PAGE.match(url)
        if found is None:
            return None
        volume = _VOLUME.match(found.group(1))
        return {KEY: volume.group(1)} if volume else None


def _check_status(status: int) -> None:
    if status in (401, 403):
        raise ProviderUnavailable("unauthorized")
    if status == 429:
        raise ProviderUnavailable("rate_limited")
    if status >= 500:
        raise ProviderUnavailable("http_503")
    if status != 200:
        raise ProviderUnavailable("schema_mismatch")


def _check_body_status(code: object) -> None:
    """ComicVine says its errors inside a 200 answer: 100 is a key it does not know, 107 the hourly
    limit. The words of its `error` are not repeated anywhere."""
    if code is None:
        return
    number = code if isinstance(code, int) and not isinstance(code, bool) else None
    if number == 1:
        return
    if number == 100:
        raise ProviderUnavailable("unauthorized")
    if number == 107:
        raise ProviderUnavailable("rate_limited")
    raise ProviderUnavailable("schema_mismatch")


def _volume_page(item: dict[str, Any]) -> str | None:
    """The page of the volume on the site: the address the answer gives when it is one, else the
    one its number makes. Anything else (another host, a query, a port) is not kept."""
    given = item.get("site_detail_url")
    if isinstance(given, str) and _SITE_PAGE.match(given):
        return given
    ident = item.get("id")
    if isinstance(ident, bool):
        return None
    if isinstance(ident, int) and 0 < ident < 10**12:
        return f"{SITE}/volume/4050-{ident}/"
    if isinstance(ident, str) and _DIGITS.match(ident):
        return f"{SITE}/volume/4050-{int(ident)}/"
    return None


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(_CONTROLS.sub("", value).split())[:limit].strip()
    return cleaned or None


def _year(value: object) -> str | None:
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if isinstance(text, str) and len(text) == 4 and text.isdigit() and 1800 <= int(text) <= 2200:
        return text
    return None
