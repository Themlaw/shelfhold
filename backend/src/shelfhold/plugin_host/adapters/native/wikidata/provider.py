# SPDX-License-Identifier: MIT
"""The Wikidata provider: correspondences, as suggestions only (EF-BIB-003, EF-BIB-013).

Wikidata is not a pivot of identity: its coverage of this corpus is too thin to name anything by
itself, and an item is only ever a guess. What it gives are correspondences: the link of the item
and the identifiers other catalogues hold for it (VIAF, the BnF, OpenLibrary). They go out as a
`RelationProposal` of low confidence, never as a field and never as an identifier of the target:
the provider declares no field, so the host keeps nothing, and the identity stays the internal
ULID. The host tells the suggestion; an admin may accept it.

Two cautions keep a suggestion honest. A label that names two items gives nothing (no guess
between them). And what comes back is trusted for its shape only: an address that is not an item
of Wikidata, or an identifier that is not shaped like its kind, is dropped.

The endpoint is the public SPARQL service (`query.wikidata.org/sparql`); nothing is written here.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.metadata import (
    EntityRef,
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

KEY = "wikidata"
SPARQL_HOST = "query.wikidata.org"
SITE = "https://www.wikidata.org"
LICENCE = "CC0-1.0"
ATTRIBUTION = "Data from Wikidata (wikidata.org), CC0 1.0"
MAX_QUERY = 200  # characters of what a person typed that go into a query
MAX_ROWS = 5
CONFIDENCE = 0.5  # a suggestion: never above this, never a certainty
_QID = re.compile(r"^Q\d{1,12}$", re.IGNORECASE)
_ITEM = re.compile(r"^https?://www\.wikidata\.org/entity/(Q\d{1,12})$")
# What each correspondence must look like to be believed (VIAF, BnF authority number, OpenLibrary).
_SHAPES = {
    "viaf": re.compile(r"^\d{1,20}$"),
    "bnf": re.compile(r"^\d{8}[0-9a-z]$"),
    "openlibrary": re.compile(r"^OL\d{1,12}[AMW]$"),
}
# Kinds of item a title may name: book, literary work, manga, comic book.
_CLASSES = "wd:Q571 wd:Q7725634 wd:Q8274 wd:Q1760610"
_CORRESPONDENCES = (
    "OPTIONAL { ?item wdt:P214 ?viaf } "
    "OPTIONAL { ?item wdt:P268 ?bnf } "
    "OPTIONAL { ?item wdt:P648 ?openlibrary } "
)


def build_manifest() -> MetadataManifest:
    return MetadataManifest(
        key=KEY,
        version="0.1.0",
        api=API_VERSION,
        name="Wikidata",
        permissions=Permissions(domains=[DomainRule(pattern=SPARQL_HOST, purpose="api")]),
        terms_url="https://www.wikidata.org/wiki/Wikidata:Data_access",
        respects_robots=True,
        attribution=ATTRIBUTION,
        homepage=SITE,
        capabilities=ProviderCapability.IDENTIFY,  # suggestions only, never a pivot
        touched_fields=set(),  # not one field: the host keeps nothing it says
        entity_types={"series", "work", "edition", "volume"},
        langs=[],
        quota=ProviderQuota(requests_per_second=1.0, burst=1),
    )


def _clean(value: str) -> str:
    """What a person typed, fit for a query: no control character, one space between words, and
    bounded."""
    printable = "".join(c if c.isprintable() else " " for c in value)
    return " ".join(printable.split())[:MAX_QUERY]


def escape_sparql(value: str) -> str:
    """A value for a SPARQL string literal."""
    return re.sub(r'([\\"])', r"\\\1", _clean(value))


def _value(binding: dict[str, Any], key: str) -> str | None:
    value = binding.get(key)
    text = value.get("value") if isinstance(value, dict) else None
    return text.strip() or None if isinstance(text, str) else None


def _checked(status: int) -> bool:
    """True: read the body. False: nothing to read. A 5xx or a 429 is the provider being down."""
    if status >= 500 or status == 429:
        raise ProviderUnavailable("http_503")
    return status == 200


def _correspondences(binding: dict[str, Any]) -> dict[str, str]:
    found = {k: _value(binding, k) for k in _SHAPES}
    return {k: v for k, v in found.items() if v is not None and _SHAPES[k].fullmatch(v)}


class WikidataProvider:
    def __init__(self) -> None:
        self.manifest = build_manifest()

    async def identify(self, f: Fetcher, target: EntityRef, hints: MatchHints) -> IdentifyResult:
        title = _clean(hints.title or "") if hints is not None else ""
        if title:
            return await self._by_title(f, target, title)
        for value in target.ids.values():
            if _QID.fullmatch(value):
                return await self._by_qid(f, target, value.upper())
        return IdentifyResult()

    async def _rows(self, f: Fetcher, query: str) -> list[dict[str, Any]]:
        url = f"https://{SPARQL_HOST}/sparql?{urlencode({'query': query, 'format': 'json'})}"
        response = await f.get(url, headers={"Accept": "application/sparql-results+json"})
        if not _checked(response.status):
            return []
        try:
            bindings = json.loads(response.body)["results"]["bindings"]
        except (ValueError, KeyError, TypeError):
            raise ProviderUnavailable("schema_mismatch") from None
        if not isinstance(bindings, list):
            raise ProviderUnavailable("schema_mismatch")
        return [b for b in bindings if isinstance(b, dict)]

    async def _by_title(self, f: Fetcher, target: EntityRef, title: str) -> IdentifyResult:
        label = escape_sparql(title)
        query = (
            "SELECT ?item ?viaf ?bnf ?openlibrary WHERE { "
            f'VALUES ?label {{ "{label}"@en "{label}"@fr }} '
            "?item rdfs:label ?label . "
            f"?item wdt:P31 ?class . VALUES ?class {{ {_CLASSES} }} "
            f"{_CORRESPONDENCES}"
            f'SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,fr". }} '
            f"}} LIMIT {MAX_ROWS}"
        )
        items: dict[str, dict[str, Any]] = {}
        for row in await self._rows(f, query):
            if (match := _ITEM.match(_value(row, "item") or "")) is not None:
                items.setdefault(match.group(1), row)
        if len(items) != 1:  # none, or a label that names several: no guess
            return IdentifyResult()
        ((qid, row),) = items.items()
        return self._suggestion(target, qid, _correspondences(row))

    async def _by_qid(self, f: Fetcher, target: EntityRef, qid: str) -> IdentifyResult:
        query = (
            "SELECT ?item ?viaf ?bnf ?openlibrary WHERE { "
            f"BIND(wd:{qid} AS ?item) "
            f"{_CORRESPONDENCES}"
            "} LIMIT 1"
        )
        rows = await self._rows(f, query)
        found = _correspondences(rows[0]) if rows else {}
        # An item already known is worth a suggestion only for what it adds.
        return self._suggestion(target, qid, found) if found else IdentifyResult()

    @staticmethod
    def _suggestion(target: EntityRef, qid: str, found: dict[str, str]) -> IdentifyResult:
        url = f"{SITE}/entity/{qid}"
        provenance = Provenance(
            provider=KEY,
            fetched_at=datetime.now(UTC),
            source_url=url,
            licence=LICENCE,
            attribution=ATTRIBUTION,
        )
        data: dict[str, Any] = {"url": url}
        if found:
            data["correspondences"] = found
        link = RelationProposal(
            kind="official_link", subject=target, data=data, confidence=CONFIDENCE,
            provenance=provenance,
        )  # fmt: skip
        return IdentifyResult(relations=[link])

    async def get_cover(self, f: Fetcher, target: EntityRef) -> ImageRequest | None:
        return None

    async def login(self, f: Fetcher, flow: AuthFlow, creds: dict[str, Any]) -> SessionState:
        raise NotImplementedError("Wikidata is read anonymously")

    async def refresh(self, f: Fetcher, s: SessionState) -> SessionState:
        raise NotImplementedError("Wikidata is read anonymously")

    def url_for(self, ids: dict[str, str]) -> str | None:
        for key in ("wikidata", "qid"):
            value = ids.get(key)
            if value and _QID.fullmatch(value):
                return f"{SITE}/entity/{value.upper()}"
        return None

    def ids_from_url(self, url: str) -> dict[str, str] | None:
        for prefix in (f"{SITE}/entity/", f"{SITE}/wiki/"):
            if url.startswith(prefix):
                value = url.removeprefix(prefix).split("/")[0].split("?")[0].split("#")[0]
                if _QID.fullmatch(value):
                    return {"wikidata": value.upper()}
        return None
