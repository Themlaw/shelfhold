# SPDX-License-Identifier: MIT
"""The OpenLibrary provider: an ISBN in, proposals out (EF-BIB-013, ENF-LEG-005).

One call to the books API (`api/books?jscmd=data`) answers an ISBN or an OpenLibrary id with the
title, the authors' names, the publisher and the date. Nothing is written here: the host decides
what to keep, never replaces a locked or hand-typed value, and stores an age only as a suggestion.
The provider never proposes an age of its own: the one thing it says about it is that a subject
which marks adult content (erotica) suggests 18+; "juvenile" never lowers anything.

Quota (D7): 1 request per second as an anonymous agent, 3 when the instance configured a contact.
The host enforces it; the provider does not limit itself.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

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
    RelationProposal,
)
from shelfhold.sdk.models import API_VERSION, AuthFlow, DomainRule, Permissions, SessionState
from shelfhold.sdk.refs import ImageRequest

KEY = "openlibrary"
API_HOST = "openlibrary.org"
COVER_HOST = "covers.openlibrary.org"
ATTRIBUTION = "Data from Open Library (openlibrary.org), CC0 1.0"
LICENCE = "CC0-1.0"
_CONFIDENCE = 0.8
_DAILY_CAP = 5_000
_ISBN = re.compile(r"^(?:\d{13}|\d{9}[\dXx])$")
_OL_ID = re.compile(r"^OL\d{1,12}M$")
# What marks adult content in an OpenLibrary subject; the only age the provider ever suggests.
_ADULT_SUBJECTS = frozenset({"erotica", "pornography", "sexually explicit"})


def _quota() -> ProviderQuota:
    return ProviderQuota(
        requests_per_second=1.0, burst=1, daily_cap=_DAILY_CAP,
        requests_per_second_with_contact=3.0,
    )  # fmt: skip


def build_manifest() -> MetadataManifest:
    return MetadataManifest(
        key=KEY,
        version="0.1.0",
        api=API_VERSION,
        name="Open Library",
        permissions=Permissions(
            domains=[
                DomainRule(pattern=API_HOST, purpose="api"),
                DomainRule(pattern=COVER_HOST, purpose="covers"),
                DomainRule(pattern="*.archive.org", purpose="covers"),  # where covers redirect
            ]
        ),
        terms_url="https://openlibrary.org/developers/api",
        respects_robots=True,
        attribution=ATTRIBUTION,
        homepage="https://openlibrary.org",
        capabilities=ProviderCapability.IDENTIFY | ProviderCapability.COVER,
        touched_fields={"title", "contributors", "publisher", "published_on", "subjects"},
        entity_types={"edition", "volume"},
        langs=[],
        quota=_quota(),
    )


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _names(items: Any, key: str = "name") -> list[str]:
    if not isinstance(items, list):
        return []
    names = [_text(i.get(key)) for i in items if isinstance(i, dict)]
    return [n for n in names if n]


class OpenLibraryProvider:
    def __init__(self) -> None:
        self.manifest = build_manifest()

    # -- the question ------------------------------------------------------------------------
    @staticmethod
    def _bibkey(target: EntityRef) -> str | None:
        for scheme in ("isbn13", "isbn10"):
            value = target.ids.get(scheme)
            if value and _ISBN.fullmatch(value):
                return f"ISBN:{value}"
        olid = target.ids.get("openlibrary")
        if olid and _OL_ID.fullmatch(olid):
            return f"OLID:{olid}"
        return None

    async def identify(self, f: Fetcher, target: EntityRef, hints: MatchHints) -> IdentifyResult:
        bibkey = self._bibkey(target)
        if bibkey is None:
            return IdentifyResult()
        url = f"https://{API_HOST}/api/books?bibkeys={bibkey}&format=json&jscmd=data"
        response = await f.get(url)
        if response.status >= 500 or response.status == 429:
            raise ProviderUnavailable("http_503")  # a 5xx, or a 429: it asks us to stop
        if response.status != 200:
            return IdentifyResult()
        try:
            data = json.loads(response.body)
        except ValueError:
            raise ProviderUnavailable("schema_mismatch") from None
        book = data.get(bibkey) if isinstance(data, dict) else None
        if not isinstance(book, dict):
            return IdentifyResult()  # not in OpenLibrary
        return self._proposals(book, target)

    def _proposals(self, book: dict[str, Any], target: EntityRef) -> IdentifyResult:
        key = _text(book.get("key"))
        source = f"https://{API_HOST}{key}" if key and key.startswith("/books/OL") else None
        provenance = Provenance(
            provider=KEY,
            fetched_at=datetime.now(UTC),
            source_url=source,
            licence=LICENCE,
            attribution=ATTRIBUTION,
        )

        def propose(field: str, value: Any) -> FieldProposal:
            return FieldProposal(
                entity_type=target.entity_type, field=field, value=value,
                confidence=_CONFIDENCE, provenance=provenance,
            )  # fmt: skip

        found: list[FieldProposal] = []
        if title := _text(book.get("title")):
            found.append(propose("title", title))
        if authors := _names(book.get("authors")):
            found.append(propose("contributors", authors))
        if publishers := _names(book.get("publishers")):
            found.append(propose("publisher", publishers[0]))
        if date := _text(book.get("publish_date")):
            found.append(propose("published_on", date))
        subjects = _names(book.get("subjects"))
        if subjects:
            found.append(propose("subjects", subjects))
        relations: list[RelationProposal] = []
        if source:
            relations.append(
                RelationProposal(kind="official_link", subject=target, data={"url": source},
                                 confidence=1.0, provenance=provenance)
            )  # fmt: skip
        if any(s.lower() in _ADULT_SUBJECTS for s in subjects):
            relations.append(
                RelationProposal(kind="age_suggestion", subject=target,
                                 data={"min_age": 18, "x": False},
                                 confidence=0.5, provenance=provenance)
            )  # fmt: skip
        return IdentifyResult(proposals=found, relations=relations)

    # -- the cover: a reference, never bytes -------------------------------------------------
    async def get_cover(self, f: Fetcher, target: EntityRef) -> ImageRequest | None:
        for scheme in ("isbn13", "isbn10"):
            value = target.ids.get(scheme)
            if value and _ISBN.fullmatch(value):
                return ImageRequest(url=f"https://{COVER_HOST}/b/isbn/{value}-L.jpg?default=false")
        return None

    # -- no account: the contract's two optional members, never used (the manifest has no `auth`)
    async def login(self, f: Fetcher, flow: AuthFlow, creds: dict[str, Any]) -> SessionState:
        raise NotImplementedError("OpenLibrary is read anonymously")

    async def refresh(self, f: Fetcher, s: SessionState) -> SessionState:
        raise NotImplementedError("OpenLibrary is read anonymously")

    # -- identifiers and addresses -----------------------------------------------------------
    def url_for(self, ids: dict[str, str]) -> str | None:
        for scheme in ("isbn13", "isbn10"):
            value = ids.get(scheme)
            if value and _ISBN.fullmatch(value):
                return f"https://{API_HOST}/isbn/{value}"
        olid = ids.get("openlibrary")
        if olid and _OL_ID.fullmatch(olid):
            return f"https://{API_HOST}/books/{olid}"
        return None

    def ids_from_url(self, url: str) -> dict[str, str] | None:
        prefix = f"https://{API_HOST}/"
        if not url.startswith(prefix):
            return None
        parts = url.removeprefix(prefix).split("/")
        if len(parts) >= 2 and parts[0] == "isbn" and len(parts) == 2 and _ISBN.fullmatch(parts[1]):
            return {"isbn13" if len(parts[1]) == 13 else "isbn10": parts[1]}
        if len(parts) >= 2 and parts[0] == "books" and _OL_ID.fullmatch(parts[1]):
            return {"openlibrary": parts[1]}
        return None
