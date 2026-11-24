# SPDX-License-Identifier: MIT
"""Source d'exemple : un petit catalogue JSON qui donne des livres EPUB.

Synthetique et hors ligne : aucun site reel, aucune URL reelle. Le catalogue est servi par un
serveur local dans les tests. La source declare un etat d'attente (`catalogue_stale`) dont la
sortie est `recheck`, et publie dans l'espace `plugin.<cle>.*`. Elle implemente `fetch_range`.
"""

from __future__ import annotations

import json
from typing import Any

from shelfhold_sdk.connected import ConnectedCapability, ServiceHealth
from shelfhold_sdk.fetcher import Fetcher
from shelfhold_sdk.models import (
    API_VERSION,
    Cursor,
    DomainRule,
    Page,
    Permissions,
    PluginKind,
    PluginWaitingState,
    RateLimit,
)
from shelfhold_sdk.refs import (
    FileAsset,
    ImageRequest,
    RangedPayload,
    RangeRef,
    SeriesInfo,
    SeriesRef,
    UnitInfo,
    UnitPayload,
    UnitRef,
)
from shelfhold_sdk.source import Capability, SourceChanged, SourceManifest

KEY = "exemple"
HOST = "catalogue.exemple.invalid"
CATALOG_URL = f"https://{HOST}/catalogue.json"
HEALTH_URL = f"https://{HOST}/health"


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
        attribution="Catalogue synthetique, licence MIT.",
        langs=["fr", "en"],
        content_kinds={"file"},
        url_patterns=[],
        capabilities=Capability.SEARCH | Capability.RANGED_CONTENT,
        rate_limit=RateLimit(requests_per_second=1.0, concurrency=1),
        rights_basis="licence",
        content_trust="untrusted",
        waiting_states=[PluginWaitingState(name="catalogue_stale", exit="recheck")],
    )


class ExampleSource:
    def __init__(self) -> None:
        self.manifest = build_manifest()
        self.connected = ConnectedCapability.SERVER_QUEUE | ConnectedCapability.HEALTH_PROBE
        self._tickets: dict[str, tuple[Any, int]] = {}
        self._counter = 0

    # --- helpers ---------------------------------------------------------------
    async def _catalog(self, f: Fetcher) -> list[dict[str, Any]]:
        response = await f.get(CATALOG_URL)
        try:
            books = json.loads(response.body)["books"]
            return [dict(b) for b in books if b.get("id") and b.get("title")]
        except (ValueError, KeyError, TypeError) as exc:
            raise SourceChanged("le catalogue n'a plus la forme attendue") from exc

    async def _book(self, f: Fetcher, ref: SeriesRef) -> dict[str, Any]:
        for book in await self._catalog(f):
            if str(book["id"]) == ref.remote_id:
                return book
        raise SourceChanged(f"le livre {ref.remote_id} n'est plus au catalogue")

    def _series(self, book: dict[str, Any]) -> SeriesRef:
        return SeriesRef(source_key=KEY, remote_id=str(book["id"]), title=str(book["title"]))

    def _units(self, book: dict[str, Any]) -> list[UnitInfo]:
        count = int(book.get("chapters", 1))
        units: list[UnitInfo] = []
        for n in range(1, count + 1):
            units.append(
                UnitInfo(
                    remote_unit_id=f"{KEY}:{book['id']}:ch-{n}",
                    source_order=n,
                    number=str(n),
                    lang="fr",
                )
            )
        return units

    def _asset(self, book: dict[str, Any]) -> FileAsset:
        return FileAsset(
            request=ImageRequest(url=str(book["file"])),
            mime="application/epub+zip",
            filename=f"{book['id']}.epub",
        )

    # --- Source ----------------------------------------------------------------
    def filters(self) -> list[Any]:
        return []

    async def sub_sources(self, f: Fetcher) -> list[Any]:
        return []

    async def search(
        self,
        f: Fetcher,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        books = await self._catalog(f)
        if not books:
            try:
                from shelfhold.plugin_host.rpc import plugin_side as _plugin_side

                await _plugin_side.publish_event("catalogue_stale", {})
            except Exception:  # noqa: S110
                pass
        needle = (q or "").casefold()
        items = [self._series(b) for b in books if needle in str(b["title"]).casefold()]
        if not items and books:
            items = [self._series(b) for b in books]
        return Page[SeriesRef](items=items, next=None)

    async def resolve_url(self, f: Fetcher, url: str) -> SeriesRef | UnitInfo | None:
        return None

    async def get_series(self, f: Fetcher, ref: SeriesRef) -> SeriesInfo:
        book = await self._book(f, ref)
        author = str(book.get("author", ""))
        return SeriesInfo(ref=ref, title=ref.title, authors=[author] if author else [])

    async def list_units(self, f: Fetcher, ref: SeriesRef) -> list[UnitInfo]:
        book = await self._book(f, ref)
        return self._units(book)

    async def fetch_unit(self, f: Fetcher, ref: SeriesRef, u: UnitInfo) -> UnitPayload:
        book = await self._book(f, ref)
        return self._asset(book)

    async def fetch_range(self, f: Fetcher, target: RangeRef) -> RangedPayload:
        book = await self._book(f, target.series)
        units = self._units(book)
        window = [
            u
            for u in units
            if target.first.source_order <= u.source_order <= target.last.source_order
        ]
        if not window:
            window = units
        ids = [u.remote_unit_id for u in window]
        return RangedPayload(
            asset=self._asset(book),
            covers=ids,
            bounds={i: (f"{i}#start", f"{i}#end") for i in ids},
            volume_closed=True,
        )

    def migrate_ref(self, old_source_key: str, old_remote_id: str) -> tuple[str, str] | None:
        return None

    def migrate_memo(self, version: int, memo: dict[str, Any]) -> dict[str, Any]:
        return memo

    # --- ConnectedService: SERVER_QUEUE ----------------------------------------
    async def request(self, f: Fetcher, target: UnitRef | RangeRef) -> Any:
        from shelfhold_sdk.connected import Ticket

        self._counter += 1
        token = f"t{self._counter}"
        self._tickets[token] = (target, 0)
        kind = "range" if isinstance(target, RangeRef) else "unit"
        return Ticket(token=token, target_kind=kind)

    async def poll(self, f: Fetcher, t: Any) -> str:
        target, polls = self._tickets[t.token]
        self._tickets[t.token] = (target, polls + 1)
        return ("queued", "downloading", "ready")[min(polls, 2)]

    async def fetch(self, f: Fetcher, t: Any) -> UnitPayload | RangedPayload:
        target, _ = self._tickets[t.token]
        if isinstance(target, RangeRef):
            return await self.fetch_range(f, target)
        return await self.fetch_unit(f, target.series, target.unit)

    async def cancel(self, f: Fetcher, t: Any) -> None:
        target, _ = self._tickets[t.token]
        self._tickets[t.token] = (target, -1)

    # --- ConnectedService: HEALTH_PROBE ---------------------------------------
    async def semantic_health(self, f: Fetcher) -> ServiceHealth:
        response = await f.get(HEALTH_URL)
        return ServiceHealth(ok=response.status == 200, version="1.0.0")

    async def request_refresh(self, f: Fetcher, refs: list[SeriesRef]) -> None:
        return None

    async def unloaded_sources(self, f: Fetcher, source_keys: list[str]) -> list[str]:
        return []

    async def download_queue_size(self, f: Fetcher) -> int:
        return 0

    async def blocking_update_filters(self, f: Fetcher) -> list[str]:
        return []

    def reinstall(self) -> ExampleSource:
        return ExampleSource()
