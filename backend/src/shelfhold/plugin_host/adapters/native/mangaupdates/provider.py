# SPDX-License-Identifier: MIT
"""The MangaUpdates provider: a title in, proposals and the latest release out (EF-BIB-013).

Two requests for one question: a search by title, then the releases of the series found. The
attribution the terms ask for travels with every proposal and with the release (`Provenance`),
so the host shows it wherever the data is shown. The release goes out as a `release` relation:
the host caches it as the calendar of the series and `series.stale` reads it there.

Quota: MangaUpdates documents no figure; the bucket is 1 request a second (the host enforces it).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
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

KEY = "mangaupdates"
API_HOST = "api.mangaupdates.com"
SITE = "https://www.mangaupdates.com"
ATTRIBUTION = "Data from MangaUpdates (mangaupdates.com)"
PER_PAGE = 5
_CONFIDENCE = 0.8
_SERIES_ID = re.compile(r"^\d{1,12}$")
_SERIES_URL = re.compile(rf"^{re.escape(SITE)}/series/(\d{{1,12}})(?:/[^/?#]*)?$")
_TAGS = re.compile(r"<[^>]+>")


def build_manifest() -> MetadataManifest:
    return MetadataManifest(
        key=KEY,
        version="0.1.0",
        api=API_VERSION,
        name="MangaUpdates",
        permissions=Permissions(domains=[DomainRule(pattern=API_HOST, purpose="api")]),
        terms_url="https://api.mangaupdates.com/",
        respects_robots=True,
        attribution=ATTRIBUTION,
        homepage=SITE,
        capabilities=ProviderCapability.IDENTIFY,
        touched_fields={"title", "description", "published_on", "subjects"},
        entity_types={"edition", "series"},
        langs=[],
        quota=ProviderQuota(requests_per_second=1.0, burst=1),
    )


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _results(response_body: bytes) -> list[dict[str, Any]]:
    try:
        results = json.loads(response_body)["results"]
    except (ValueError, KeyError, TypeError):
        raise ProviderUnavailable("schema_mismatch") from None
    if not isinstance(results, list):
        raise ProviderUnavailable("schema_mismatch")
    return [r for r in results if isinstance(r, dict)]


def _checked(status: int) -> bool:
    """True: read the body. False: nothing to read. A 5xx or a 429 is the provider being down."""
    if status >= 500 or status == 429:
        raise ProviderUnavailable("http_503")
    return status == 200


class MangaUpdatesProvider:
    def __init__(self) -> None:
        self.manifest = build_manifest()

    async def identify(self, f: Fetcher, target: EntityRef, hints: MatchHints) -> IdentifyResult:
        asked = _text(hints.title)
        if asked is None:
            return IdentifyResult()
        response = await f.post(
            f"https://{API_HOST}/v1/series/search", json={"search": asked, "perpage": PER_PAGE}
        )
        if not _checked(response.status):
            return IdentifyResult()
        record = next(
            (r["record"] for r in _results(response.body)
             if isinstance(r.get("record"), dict) and _text(r["record"].get("title"))
             and r["record"]["title"].strip().casefold() == asked.casefold()
             and _SERIES_ID.match(str(r["record"].get("series_id", "")))),
            None,
        )  # fmt: skip
        if record is None:
            return IdentifyResult()  # only the title asked for, never a near miss
        series_id = str(record["series_id"])
        source = f"{SITE}/series/{series_id}"
        provenance = Provenance(
            provider=KEY, fetched_at=datetime.now(UTC), source_url=source, attribution=ATTRIBUTION
        )
        result = self._proposals(record, target, provenance)
        release = await self._latest_release(f, target, series_id, provenance)
        if release is not None:
            result.relations.append(release)
        return result

    @staticmethod
    def _proposals(
        record: dict[str, Any], target: EntityRef, provenance: Provenance
    ) -> IdentifyResult:
        def propose(field: str, value: Any) -> FieldProposal:
            return FieldProposal(
                entity_type=target.entity_type, field=field, value=value,
                confidence=_CONFIDENCE, provenance=provenance,
            )  # fmt: skip

        found = [propose("title", record["title"].strip())]
        if summary := _text(record.get("description")):
            found.append(propose("description", _TAGS.sub("", summary).strip()))
        year = str(record.get("year", ""))
        if year.isdigit() and len(year) == 4:
            found.append(propose("published_on", year))
        genres = record.get("genres")
        names = (
            [g["genre"].strip() for g in genres if isinstance(g, dict) and _text(g.get("genre"))]
            if isinstance(genres, list)
            else []
        )
        if names:
            found.append(propose("subjects", names))
        return IdentifyResult(proposals=found)

    async def _latest_release(
        self, f: Fetcher, target: EntityRef, series_id: str, provenance: Provenance
    ) -> RelationProposal | None:
        response = await f.post(
            f"https://{API_HOST}/v1/releases/search",
            json={"search": series_id, "search_type": "series", "perpage": PER_PAGE},
        )
        if not _checked(response.status):
            return None
        latest: tuple[date, dict[str, Any]] | None = None
        for item in _results(response.body):
            record, meta = item.get("record"), item.get("metadata")
            ident = (
                ((meta or {}).get("series") or {}).get("series_id")
                if isinstance(meta, dict)
                else None
            )
            if not isinstance(record, dict) or str(ident) != series_id:
                continue  # a release of another series is never this one's calendar
            try:
                released = date.fromisoformat(str(record.get("release_date")))
            except ValueError:
                continue
            if latest is None or released > latest[0]:
                latest = (released, record)
        if latest is None:
            return None
        released, record = latest
        data: dict[str, Any] = {"series_id": series_id, "released_on": released.isoformat()}
        for key in ("chapter", "volume"):
            if value := _text(record.get(key)):
                data[key] = value
        return RelationProposal(kind="release", subject=target, data=data, confidence=0.9,
                                provenance=provenance)  # fmt: skip

    async def get_cover(self, f: Fetcher, target: EntityRef) -> ImageRequest | None:
        return None

    async def login(self, f: Fetcher, flow: AuthFlow, creds: dict[str, Any]) -> SessionState:
        raise NotImplementedError("MangaUpdates is read anonymously")

    async def refresh(self, f: Fetcher, s: SessionState) -> SessionState:
        raise NotImplementedError("MangaUpdates is read anonymously")

    def url_for(self, ids: dict[str, str]) -> str | None:
        found = ids.get("mangaupdates")
        return f"{SITE}/series/{found}" if found and _SERIES_ID.match(found) else None

    def ids_from_url(self, url: str) -> dict[str, str] | None:
        match = _SERIES_URL.match(url)
        return {"mangaupdates": match.group(1)} if match else None
