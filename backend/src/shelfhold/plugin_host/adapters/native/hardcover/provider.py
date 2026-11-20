# SPDX-License-Identifier: MIT
"""The Hardcover provider: a title or an ISBN in, proposals out (EF-BIB-013, ENF-LEG-005).

Hardcover's API is GraphQL, authorised by a token that belongs to the instance's admin account.
That token is only ever used here, by the server: the host seals it at rest, hands it over as a
value, and the Fetcher sends it in an `authorization` header (a Fetcher logs the route only, and
never files a request that carries a credential). This module never puts it in a message, an
error, a repr or a result. What a person typed travels as a GraphQL *variable*, never glued into
the query; what comes back is believed for its shape only.

Quota: 60 requests a minute and 5 000 a day, held by the host (`ProviderQuota`), not by the
provider. The API is read-only here: a search, nothing is written, no list or shelf of anyone.
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
)
from shelfhold_sdk.models import API_VERSION, AuthFlow, DomainRule, Permissions, SessionState
from shelfhold_sdk.refs import ImageRequest

KEY = "hardcover"
API_HOST = "api.hardcover.app"
API_URL = f"https://{API_HOST}/v1/graphql"
SITE = "https://hardcover.app"
TERMS_URL = "https://docs.hardcover.app/api/getting-started/"
ATTRIBUTION = "Data from Hardcover (hardcover.app)"
LICENCE = "Hardcover API"
REQUESTS_PER_MINUTE = 60
REQUESTS_PER_DAY = 5000
PER_PAGE = 5  # a lookup, never a collection
MAX_QUERY = 200  # characters of what a person typed
MAX_TEXT = 2000
CONFIDENCE = 0.8
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,199}$")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SEARCH = """
query Lookup($q: String!, $perPage: Int!) {
  search(query: $q, query_type: "Book", per_page: $perPage, page: 1) {
    results
  }
}
"""


def build_manifest() -> MetadataManifest:
    return MetadataManifest(
        key=KEY,
        version="0.1.0",
        api=API_VERSION,
        name="Hardcover",
        permissions=Permissions(domains=[DomainRule(pattern=API_HOST, purpose="api")]),
        terms_url=TERMS_URL,
        respects_robots=True,
        attribution=ATTRIBUTION,
        homepage=SITE,
        capabilities=ProviderCapability.IDENTIFY,  # no cover, no account, no list
        touched_fields={"title", "description", "publisher", "published_on"},
        entity_types={"series", "work", "edition", "volume"},
        langs=[],
        quota=ProviderQuota(
            requests_per_second=REQUESTS_PER_MINUTE / 60, burst=1, daily_cap=REQUESTS_PER_DAY
        ),
        settings_schema={
            "type": "object",
            "required": ["token"],
            "properties": {
                "token": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                    "title": "Hardcover API token (the admin's, kept on the server)",
                }
            },
        },
    )


class HardcoverProvider:
    def __init__(self, *, token: str | SecretStr | None = None) -> None:
        self.manifest = build_manifest()
        text = token.get_secret_value() if isinstance(token, SecretStr) else token
        text = (text or "").strip()
        self._token = SecretStr(text) if text else None  # a SecretStr: no repr gives it away

    def __repr__(self) -> str:
        return f"HardcoverProvider(token={'set' if self._token else 'unset'})"

    async def identify(self, f: Fetcher, target: EntityRef, hints: MatchHints) -> IdentifyResult:
        if self._token is None:
            raise ProviderUnavailable("unauthorized")  # nothing is asked without the token
        question = self._question(target, hints)
        if question is None:
            return IdentifyResult()
        response = await f.post(
            API_URL,
            json={"query": _SEARCH, "variables": {"q": question, "perPage": PER_PAGE}},
            headers={
                "authorization": f"Bearer {self._token.get_secret_value()}",
                "accept": "application/json",
            },
        )
        _check_status(response.status)
        try:
            data = json.loads(response.body)
        except (ValueError, TypeError):
            raise ProviderUnavailable("schema_mismatch") from None
        if not isinstance(data, dict):
            raise ProviderUnavailable("schema_mismatch")
        _check_errors(data.get("errors"))
        document = _first_document(data)
        if document is None:
            return IdentifyResult()  # nothing known: no guess
        return self._proposals(target, document)

    @staticmethod
    def _question(target: EntityRef, hints: MatchHints | None) -> str | None:
        """The ISBN of the Edition when it has one (exact), else the title that was given."""
        for scheme in ("isbn13", "isbn10"):
            value = target.ids.get(scheme)
            if value and value.isalnum() and len(value) in (10, 13):
                return value
        return _text(hints.title if hints is not None else None, MAX_QUERY)

    def _proposals(self, target: EntityRef, item: dict[str, Any]) -> IdentifyResult:
        slug = item.get("slug")
        page = f"{SITE}/books/{slug}" if isinstance(slug, str) and _SLUG.match(slug) else SITE
        provenance = Provenance(
            provider=KEY,
            fetched_at=datetime.now(UTC),
            source_url=page,
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
        if title := _text(item.get("title"), MAX_QUERY):
            found.append(propose("title", title))
        if description := _text(item.get("description"), MAX_TEXT):
            found.append(propose("description", description))
        if publisher := _text(item.get("publisher"), MAX_QUERY):
            found.append(propose("publisher", publisher))
        if year := _year(item.get("release_year")):
            found.append(propose("published_on", year))
        return IdentifyResult(proposals=found)

    async def get_cover(self, f: Fetcher, target: EntityRef) -> ImageRequest | None:
        return None

    async def login(self, f: Fetcher, flow: AuthFlow, creds: dict[str, Any]) -> SessionState:
        raise NotImplementedError("Hardcover uses the instance's own token")

    async def refresh(self, f: Fetcher, s: SessionState) -> SessionState:
        raise NotImplementedError("Hardcover uses the instance's own token")

    def url_for(self, ids: dict[str, str]) -> str | None:
        value = ids.get(KEY)
        return f"{SITE}/books/{value}" if value and _SLUG.match(value) else None

    def ids_from_url(self, url: str) -> dict[str, str] | None:
        found = re.match(rf"^{re.escape(SITE)}/books/([a-z0-9][a-z0-9-]{{0,199}})/?$", url)
        return {KEY: found.group(1)} if found else None


def _check_status(status: int) -> None:
    if status in (401, 403):
        raise ProviderUnavailable("unauthorized")
    if status == 429:
        raise ProviderUnavailable("rate_limited")
    if status >= 500:
        raise ProviderUnavailable("http_503")
    if status != 200:
        raise ProviderUnavailable("schema_mismatch")


def _check_errors(errors: object) -> None:
    """GraphQL says its errors inside a 200 answer. A token it does not know is `unauthorized`,
    a throttle is `rate_limited`; the words of the message are not repeated anywhere."""
    if errors is None:
        return
    if not isinstance(errors, list):
        raise ProviderUnavailable("schema_mismatch")
    if not errors:
        return
    for error in errors:
        extensions = error.get("extensions") if isinstance(error, dict) else None
        code = extensions.get("code") if isinstance(extensions, dict) else None
        if code in ("invalid-jwt", "invalid-headers", "access-denied", "unauthorized"):
            raise ProviderUnavailable("unauthorized")
        if code in ("throttled", "rate-limited"):
            raise ProviderUnavailable("rate_limited")
    raise ProviderUnavailable("schema_mismatch")


def _first_document(data: dict[str, Any]) -> dict[str, Any] | None:
    """The first hit of the search; `None` when there is none. A shape that is not the documented
    one is `schema_mismatch`."""
    body = data.get("data")
    search = body.get("search") if isinstance(body, dict) else None
    results = search.get("results") if isinstance(search, dict) else None
    if isinstance(results, str):  # the API may hand the results as a JSON text
        try:
            results = json.loads(results)
        except ValueError:
            raise ProviderUnavailable("schema_mismatch") from None
    if not isinstance(results, dict):
        raise ProviderUnavailable("schema_mismatch")
    hits = results.get("hits")
    if not isinstance(hits, list):
        raise ProviderUnavailable("schema_mismatch")
    if not hits:
        return None
    first = hits[0]
    document = first.get("document") if isinstance(first, dict) else None
    if not isinstance(document, dict):
        raise ProviderUnavailable("schema_mismatch")
    return document


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
