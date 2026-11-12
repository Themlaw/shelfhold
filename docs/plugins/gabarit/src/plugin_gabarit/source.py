# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source d'exemple du gabarit : un petit catalogue JSON qui donne des livres EPUB.

Remplacez `HOST`, le manifeste et l'analyse du catalogue. Ne changez pas la forme : tout passe par
le `Fetcher` de l'hôte, jamais par un socket, un fichier ou un processus ouvert par le plugin.
"""

from __future__ import annotations

import json
from typing import Any

from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.models import (
    API_VERSION,
    DomainRule,
    Page,
    Permissions,
    PluginKind,
    RateLimit,
)
from shelfhold.sdk.refs import (
    FileAsset,
    ImageRequest,
    SeriesInfo,
    SeriesRef,
    UnitInfo,
    UnitPayload,
)
from shelfhold.sdk.source import Capability, SourceChanged, SourceManifest

KEY = "gabarit"
HOST = "catalogue.exemple.invalid"  # votre hôte réel : seul hôte joignable (permissions.domains)
CATALOG = f"https://{HOST}/catalogue.json"


def build_manifest() -> SourceManifest:
    return SourceManifest(
        key=KEY,
        kind=PluginKind.SOURCE,
        version="0.1.0",
        api=API_VERSION,
        name="Source d'exemple",
        permissions=Permissions(domains=[DomainRule(pattern=HOST, purpose="api")]),
        terms_url=f"https://{HOST}/conditions",
        respects_robots=True,
        attribution="Catalogue d'exemple, texte sous licence libre.",
        langs=["fr"],
        content_kinds={"file"},
        url_patterns=[],
        capabilities=Capability.SEARCH,
        rate_limit=RateLimit(requests_per_second=0.5, concurrency=1),
        rights_basis="licence",
        content_trust="untrusted",
    )


class ExampleSource:
    """Un livre est une série d'une seule Unité. L'identité de l'Unité vient du catalogue."""

    def __init__(self) -> None:
        self.manifest = build_manifest()

    async def _catalog(self, f: Fetcher) -> list[dict[str, Any]]:
        response = await f.get(CATALOG)
        try:
            books = json.loads(response.body)["books"]
            return [dict(b) for b in books if b["id"] and b["title"]]
        except (ValueError, KeyError, TypeError) as exc:
            # un format changé est `SourceChanged`, jamais un contenu vide
            raise SourceChanged("le catalogue n'a plus la forme attendue") from exc

    def _ref(self, book: dict[str, Any]) -> SeriesRef:
        return SeriesRef(source_key=KEY, remote_id=str(book["id"]), title=str(book["title"]))

    async def search(
        self, f: Fetcher, q: str, filters: dict[str, Any], cursor: Any, *, sub: str | None = None
    ) -> Page[SeriesRef]:
        books = await self._catalog(f)
        needle = q.casefold()
        return Page[SeriesRef](
            items=[self._ref(b) for b in books if needle in str(b["title"]).casefold()]
        )

    def filters(self) -> list[Any]:
        return []

    async def _book(self, f: Fetcher, ref: SeriesRef) -> dict[str, Any]:
        for book in await self._catalog(f):
            if str(book["id"]) == ref.remote_id:
                return book
        raise SourceChanged(f"le livre {ref.remote_id} n'est plus au catalogue")

    async def get_series(self, f: Fetcher, ref: SeriesRef) -> SeriesInfo:
        book = await self._book(f, ref)
        author = str(book.get("author", ""))
        return SeriesInfo(ref=ref, title=ref.title, authors=[author] if author else [])

    async def list_units(self, f: Fetcher, ref: SeriesRef) -> list[UnitInfo]:
        book = await self._book(f, ref)
        # `remote_unit_id` : l'identifiant du catalogue amont, jamais un entier de votre base locale
        return [UnitInfo(remote_unit_id=str(book["id"]), source_order=1, kind="book")]

    async def fetch_unit(self, f: Fetcher, ref: SeriesRef, u: UnitInfo) -> UnitPayload:
        book = await self._book(f, ref)
        request = ImageRequest(url=str(book["epub"]))
        return FileAsset(
            request=request, mime="application/epub+zip", filename=f"{book['id']}.epub"
        )
