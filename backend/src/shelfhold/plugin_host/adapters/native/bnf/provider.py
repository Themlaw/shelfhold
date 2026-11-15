# SPDX-License-Identifier: MIT
"""The BnF provider: a title or an ISBN in, proposals out (EF-BIB-013, ENF-LEG-005).

Two doors, one provider: the SRU of the general catalogue (`catalogue.bnf.fr/api/SRU`), by title
or by ISBN, and the SPARQL endpoint of `data.bnf.fr`, asked by ISBN when the SRU does not know it.
The re-use is under the Licence Ouverte: the source (the address of the notice) and the date of
retrieval travel with every proposal. Politeness (1 request a second, the cache) is the host's:
it lives in the Fetcher the provider is handed, never here.

Two things are never proposed. The notices and subjects of the Dewey classification: they are not
the BnF's to give. And a cover: the conditions of Gallica are distinct, so the provider has no
cover capability and keeps no Gallica address.

SRU answers are XML. A native provider may not parse it (ENF-SEC-014): the host hands it a reader.
Nothing is written here: the host decides what to keep and never replaces a locked or hand-typed
value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

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

KEY = "bnf"
SRU_HOST = "catalogue.bnf.fr"
SPARQL_HOST = "data.bnf.fr"
SITE = f"https://{SRU_HOST}"
LICENCE = "Licence Ouverte"
ATTRIBUTION = f"Data from BnF (catalogue.bnf.fr, data.bnf.fr), {LICENCE}"
MAX_QUERY = 200  # characters of what a person typed that go into a query
MAX_RECORDS = 5
MAX_LIST = 20  # contributors or subjects kept from one notice
_CONFIDENCE = 0.8
_CONFIDENCE_LINK = 0.6
_ARK = r"cb[0-9a-z]{6,12}"
_CATALOGUE_ARK = re.compile(rf"^https?://{re.escape(SRU_HOST)}/ark:/12148/({_ARK})/?$")
_DATA_ARK = re.compile(rf"^https?://{re.escape(SPARQL_HOST)}/ark:/12148/({_ARK})(?:#about)?$")
_ARK_ID = re.compile(rf"^{_ARK}$")
_ISBN = re.compile(r"^(?:\d{13}|\d{9}[\dXx])$")
_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_DDC_NUMBER = re.compile(r"^\d{3}(?:\.\d+)?$")
_DEWEY = "dewey"

SruReader = Callable[[bytes], list[dict[str, Any]]]
"""The host's reader of an SRU answer: bytes in, one plain dict per notice out. `ValueError` when
the document is not an SRU answer or not safe to read."""


def build_manifest() -> MetadataManifest:
    return MetadataManifest(
        key=KEY,
        version="0.1.0",
        api=API_VERSION,
        name="BnF",
        permissions=Permissions(
            domains=[
                DomainRule(pattern=SRU_HOST, purpose="api"),
                DomainRule(pattern=SPARQL_HOST, purpose="api"),
            ]
        ),
        terms_url="https://data.bnf.fr/fr/licence",
        respects_robots=True,
        attribution=ATTRIBUTION,
        homepage="https://www.bnf.fr",
        capabilities=ProviderCapability.IDENTIFY,  # no COVER: never from Gallica
        touched_fields={"title", "contributors", "publisher", "published_on", "subjects"},
        entity_types={"edition", "work", "series"},
        langs=[],
        quota=ProviderQuota(requests_per_second=1.0, burst=1),
    )


def _text(value: Any) -> str | None:
    return (" ".join(value.split()) or None) if isinstance(value, str) else None


def _clean(value: str) -> str:
    """What a person typed, fit for a query: no control character, one space between words, and
    bounded."""
    printable = "".join(c if c.isprintable() else " " for c in value)
    return " ".join(printable.split())[:MAX_QUERY]


def escape_sru(value: str) -> str:
    """A value for a quoted CQL term of the SRU. Besides the quote and the backslash, `*`, `?`
    and `^` are masking characters of CQL: escaped, a title is never a pattern."""
    return re.sub(r'([\\"*?^])', r"\\\1", _clean(value))


def escape_sparql(value: str) -> str:
    """A value for a SPARQL string literal."""
    return re.sub(r'([\\"])', r"\\\1", _clean(value))


def _isbn_of(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    bare = value.replace("-", "").replace(" ", "")
    return bare.upper() if _ISBN.fullmatch(bare) else None


def _checked(status: int) -> bool:
    """True: read the body. False: nothing to read. A 5xx or a 429 is the provider being down."""
    if status >= 500 or status == 429:
        raise ProviderUnavailable("http_503")
    return status == 200


def _title_proper(value: Any) -> str | None:
    """The title before the statement of responsibility (`Title / by someone`)."""
    text = _text(value)
    return _text(text.split(" / ")[0].rstrip(" .;:,")) if text else None


def _contributor(value: Any) -> str | None:
    """`Name, First (1900-1980). Role` -> `Name, First`; without dates the text stays whole."""
    text = _text(value)
    if text is None:
        return None
    return _text(text.split(" (")[0].rstrip(" .")) if " (" in text else text


def _year(value: Any) -> str | None:
    found = _YEAR.search(value) if isinstance(value, str) else None
    return found.group(1) if found else None


def _is_dewey_subject(subject: dict[str, Any]) -> bool:
    text, scheme = str(subject.get("text") or ""), str(subject.get("scheme") or "")
    return (
        _DEWEY in text.casefold()
        or _DEWEY in scheme.casefold()
        or scheme.casefold().endswith("ddc")
        or _DDC_NUMBER.fullmatch(text.strip()) is not None
    )


def _is_dewey_notice(record: dict[str, Any]) -> bool:
    return any(_DEWEY in str(t).casefold() for t in record.get("types") or [])


def _dedup(values: list[str | None]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))[:MAX_LIST]


def _value(binding: dict[str, Any], key: str) -> str | None:
    value = binding.get(key)
    return _text(value.get("value")) if isinstance(value, dict) else None


class BnFProvider:
    def __init__(self, *, reader: SruReader) -> None:
        self.manifest = build_manifest()
        self._read = reader

    async def identify(self, f: Fetcher, target: EntityRef, hints: MatchHints) -> IdentifyResult:
        title = _clean(hints.title or "") if hints is not None else ""
        if title:
            return await self._by_sru(f, target, f'bib.title all "{escape_sru(title)}"')
        for scheme in ("isbn13", "isbn10"):
            isbn = _isbn_of(target.ids.get(scheme))
            if isbn is None:
                continue
            result = await self._by_sru(f, target, f'bib.isbn all "{isbn}"')
            return result if result.proposals else await self._by_sparql(f, target, isbn)
        return IdentifyResult()

    async def _by_sru(self, f: Fetcher, target: EntityRef, query: str) -> IdentifyResult:
        params = {
            "operation": "searchRetrieve",
            "version": "1.2",
            "query": query,
            "maximumRecords": str(MAX_RECORDS),
        }
        response = await f.get(f"https://{SRU_HOST}/api/SRU?{urlencode(params)}")
        if not _checked(response.status):
            return IdentifyResult()
        try:
            records = self._read(response.body)
        except ValueError:
            raise ProviderUnavailable("schema_mismatch") from None
        for record in records:
            if _is_dewey_notice(record):
                continue  # a notice of the classification is not the BnF's to give
            if (title := _title_proper(record.get("title"))) is None:
                continue
            ark = next(
                (
                    m.group(1)
                    for i in record.get("identifiers") or []
                    if (m := _CATALOGUE_ARK.match(str(i).strip()))
                ),
                None,
            )
            subjects = [
                _text(s.get("text"))
                for s in record.get("subjects") or []
                if isinstance(s, dict) and not _is_dewey_subject(s)
            ]
            return self._proposals(
                target,
                title=title,
                creators=[_contributor(c) for c in record.get("creators") or []],
                publisher=_text(record.get("publisher")),
                date=_year(record.get("date")),
                subjects=subjects,
                notice=f"{SITE}/ark:/12148/{ark}" if ark else None,
                home=f"{SITE}/",
            )
        return IdentifyResult()

    async def _by_sparql(self, f: Fetcher, target: EntityRef, isbn: str) -> IdentifyResult:
        dc = "http://purl.org/dc/terms/"
        query = (
            "SELECT ?work ?title ?creator ?publisher ?date ?subject WHERE { "
            f'?work <{dc}identifier> "{escape_sparql(isbn)}" . '
            f"OPTIONAL {{ ?work <{dc}title> ?title }} "
            f"OPTIONAL {{ ?work <{dc}creator> ?creator }} "
            f"OPTIONAL {{ ?work <{dc}publisher> ?publisher }} "
            f"OPTIONAL {{ ?work <{dc}date> ?date }} "
            f"OPTIONAL {{ ?work <{dc}subject> ?subject }} "
            f"}} LIMIT {MAX_RECORDS}"
        )
        url = f"https://{SPARQL_HOST}/sparql?{urlencode({'query': query, 'format': 'json'})}"
        response = await f.get(url, headers={"Accept": "application/sparql-results+json"})
        if not _checked(response.status):
            return IdentifyResult()
        try:
            bindings = json.loads(response.body)["results"]["bindings"]
        except (ValueError, KeyError, TypeError):
            raise ProviderUnavailable("schema_mismatch") from None
        if not isinstance(bindings, list):
            raise ProviderUnavailable("schema_mismatch")
        rows = [b for b in bindings if isinstance(b, dict)]
        first = next((b for b in rows if _title_proper(_value(b, "title"))), None)
        if first is None:
            return IdentifyResult()
        ark = _DATA_ARK.match(_value(first, "work") or "")
        return self._proposals(
            target,
            title=_title_proper(_value(first, "title")),
            creators=[_contributor(_value(b, "creator")) for b in rows],
            publisher=_value(first, "publisher"),
            date=_year(_value(first, "date")),
            subjects=[
                s
                for s in (_value(b, "subject") for b in rows)
                if s is not None and not _is_dewey_subject({"text": s})
            ],
            notice=f"https://{SPARQL_HOST}/ark:/12148/{ark.group(1)}" if ark else None,
            home=f"https://{SPARQL_HOST}/",
        )

    @staticmethod
    def _proposals(
        target: EntityRef,
        *,
        title: str | None,
        creators: list[str | None],
        publisher: str | None,
        date: str | None,
        subjects: list[str | None],
        notice: str | None,
        home: str,
    ) -> IdentifyResult:
        provenance = Provenance(
            provider=KEY,
            fetched_at=datetime.now(UTC),
            source_url=notice or home,
            licence=LICENCE,
            attribution=ATTRIBUTION,
        )

        def propose(field: str, value: Any) -> FieldProposal:
            return FieldProposal(
                entity_type=target.entity_type,
                field=field,
                value=value,
                confidence=_CONFIDENCE,
                provenance=provenance,
            )

        found: list[FieldProposal] = []
        if title:
            found.append(propose("title", title))
        if names := _dedup(creators):
            found.append(propose("contributors", names))
        if publisher:
            found.append(propose("publisher", publisher))
        if date:
            found.append(propose("published_on", date))
        if kept := _dedup(subjects):
            found.append(propose("subjects", kept))
        relations = (
            [
                RelationProposal(
                    kind="official_link",
                    subject=target,
                    data={"url": notice},
                    confidence=_CONFIDENCE_LINK,
                    provenance=provenance,
                )
            ]
            if notice
            else []
        )
        return IdentifyResult(proposals=found, relations=relations)

    async def get_cover(self, f: Fetcher, target: EntityRef) -> ImageRequest | None:
        return None  # no cover from Gallica: distinct conditions

    async def login(self, f: Fetcher, flow: AuthFlow, creds: dict[str, Any]) -> SessionState:
        raise NotImplementedError("BnF is read anonymously")

    async def refresh(self, f: Fetcher, s: SessionState) -> SessionState:
        raise NotImplementedError("BnF is read anonymously")

    def url_for(self, ids: dict[str, str]) -> str | None:
        for key in ("bnf", "ark"):
            value = ids.get(key)
            if value and _ARK_ID.fullmatch(value):
                return f"{SITE}/ark:/12148/{value}"
        return None

    def ids_from_url(self, url: str) -> dict[str, str] | None:
        prefix = f"{SITE}/ark:/12148/"
        if not url.startswith(prefix):
            return None
        value = url.removeprefix(prefix).split("/")[0].split("?")[0].split("#")[0]
        return {"bnf": value} if _ARK_ID.fullmatch(value) else None
