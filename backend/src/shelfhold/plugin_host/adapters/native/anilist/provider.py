# SPDX-License-Identifier: MIT
"""The AniList provider: a title in, proposals out (EF-BIB-013, ENF-LEG-005).

A lookup and nothing else. One question is one search query, one request, a handful of results:
no login, no token, no list of anyone, no collection of the catalogue. Nothing is written here:
the host decides what to keep and never replaces a locked or hand-typed value.

Quota (D13): AniList announces 30 requests a minute while its API is degraded and 90 in nominal
times, in `X-RateLimit-Limit`. The host's bucket follows the header (never above 90, 30 before it
has heard one); the provider does not limit itself.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from shelfhold_sdk.fetcher import Fetcher
from shelfhold_sdk.metadata import (
    AdaptiveLimit,
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

KEY = "anilist"
API_HOST = "graphql.anilist.co"
SITE = "https://anilist.co"
ATTRIBUTION = "Data from AniList (anilist.co)"
PER_PAGE = 5  # a lookup, never a collection
_CONFIDENCE = 0.8
_MANGA_URL = re.compile(rf"^{re.escape(SITE)}/manga/(\d{{1,10}})/?$")
_TAGS = re.compile(r"<[^>]+>")
_SEARCH = """
query ($search: String, $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    media(search: $search, type: MANGA) {
      id siteUrl format status description(asHtml: false) isAdult
      title { romaji english native }
      startDate { year month day }
      genres
      staff(perPage: 5) { edges { role node { name { full } } } }
      externalLinks { url site type }
    }
  }
}
"""


def build_manifest() -> MetadataManifest:
    return MetadataManifest(
        key=KEY,
        version="0.1.0",
        api=API_VERSION,
        name="AniList",
        permissions=Permissions(domains=[DomainRule(pattern=API_HOST, purpose="api")]),
        terms_url="https://docs.anilist.co/guide/terms-of-use",
        respects_robots=True,
        attribution=ATTRIBUTION,
        homepage=SITE,
        capabilities=ProviderCapability.IDENTIFY,  # no cover, no account
        touched_fields={"title", "description", "contributors", "published_on", "subjects"},
        entity_types={"edition", "series"},
        langs=[],
        quota=ProviderQuota(
            requests_per_second=30 / 60,
            burst=1,
            adaptive=AdaptiveLimit(
                header="X-RateLimit-Limit", start_per_minute=30, max_per_minute=90
            ),
        ),
    )


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _titles(media: dict[str, Any]) -> list[str]:
    titles = media.get("title")
    found = (
        [_text(titles.get(k)) for k in ("english", "romaji", "native")]
        if isinstance(titles, dict)
        else []
    )
    return [t for t in found if t]


def _date(start: Any) -> str | None:
    if not isinstance(start, dict) or not isinstance(start.get("year"), int):
        return None
    parts = [start["year"], start.get("month"), start.get("day")]
    kept: list[int] = []
    for part in parts:
        if not isinstance(part, int):
            break
        kept.append(part)
    return "-".join(f"{n:04d}" if i == 0 else f"{n:02d}" for i, n in enumerate(kept))


class AniListProvider:
    def __init__(self) -> None:
        self.manifest = build_manifest()

    async def identify(self, f: Fetcher, target: EntityRef, hints: MatchHints) -> IdentifyResult:
        asked = _text(hints.title)
        if asked is None:
            return IdentifyResult()  # a lookup is by title: nothing to ask
        response = await f.post(
            f"https://{API_HOST}/",
            json={"query": _SEARCH, "variables": {"search": asked, "perPage": PER_PAGE}},
        )
        if response.status >= 500 or response.status == 429:
            raise ProviderUnavailable("http_503")  # it asks us to stop; the host waits
        if response.status != 200:
            return IdentifyResult()
        try:
            data = json.loads(response.body)
            media = data["data"]["Page"]["media"]
        except (ValueError, KeyError, TypeError):
            raise ProviderUnavailable("schema_mismatch") from None
        if not isinstance(media, list):
            raise ProviderUnavailable("schema_mismatch")
        wanted = asked.casefold()
        for item in media:  # only a title that is the one asked for: never a near miss
            if isinstance(item, dict) and wanted in {t.casefold() for t in _titles(item)}:
                return self._proposals(item, target)
        return IdentifyResult()

    def _proposals(self, media: dict[str, Any], target: EntityRef) -> IdentifyResult:
        site = _text(media.get("siteUrl"))
        source = site if site and _MANGA_URL.match(site) else None
        provenance = Provenance(
            provider=KEY, fetched_at=datetime.now(UTC), source_url=source, attribution=ATTRIBUTION
        )

        def propose(field: str, value: Any) -> FieldProposal:
            return FieldProposal(
                entity_type=target.entity_type, field=field, value=value,
                confidence=_CONFIDENCE, provenance=provenance,
            )  # fmt: skip

        found: list[FieldProposal] = []
        if titles := _titles(media):
            found.append(propose("title", titles[0]))
        if summary := _text(media.get("description")):
            found.append(propose("description", _TAGS.sub("", summary).strip()))
        if date := _date(media.get("startDate")):
            found.append(propose("published_on", date))
        genres = media.get("genres")
        if isinstance(genres, list) and (names := [g for g in genres if _text(g)]):
            found.append(propose("subjects", names))
        staff = _staff(media.get("staff"))
        if staff:
            found.append(propose("contributors", staff))
        relations: list[RelationProposal] = []
        for link in media.get("externalLinks") or []:
            url = _text(link.get("url")) if isinstance(link, dict) else None
            if url and url.startswith("https://"):
                relations.append(
                    RelationProposal(kind="official_link", subject=target, data={"url": url},
                                     confidence=0.6, provenance=provenance)
                )  # fmt: skip
        if media.get("isAdult") is True:
            relations.append(
                RelationProposal(kind="age_suggestion", subject=target,
                                 data={"min_age": 18, "x": False},
                                 confidence=0.5, provenance=provenance)
            )  # fmt: skip
        return IdentifyResult(proposals=found, relations=relations)

    # -- no cover: a reference to an image on another host is not something a lookup asks for ----
    async def get_cover(self, f: Fetcher, target: EntityRef) -> ImageRequest | None:
        return None

    # -- no account: the contract's two optional members, never used (the manifest has no `auth`)
    async def login(self, f: Fetcher, flow: AuthFlow, creds: dict[str, Any]) -> SessionState:
        raise NotImplementedError("AniList is only asked, never signed in to")

    async def refresh(self, f: Fetcher, s: SessionState) -> SessionState:
        raise NotImplementedError("AniList is only asked, never signed in to")

    # -- identifiers and addresses -----------------------------------------------------------
    def url_for(self, ids: dict[str, str]) -> str | None:
        found = ids.get("anilist")
        return f"{SITE}/manga/{found}" if found and found.isdigit() else None

    def ids_from_url(self, url: str) -> dict[str, str] | None:
        match = _MANGA_URL.match(url)
        return {"anilist": match.group(1)} if match else None


def _staff(staff: Any) -> list[str]:
    edges = staff.get("edges") if isinstance(staff, dict) else None
    names: list[str] = []
    for edge in edges or []:
        node = edge.get("node") if isinstance(edge, dict) else None
        name = node.get("name") if isinstance(node, dict) else None
        full = _text(name.get("full")) if isinstance(name, dict) else None
        if full and full not in names:
            names.append(full)
    return names
