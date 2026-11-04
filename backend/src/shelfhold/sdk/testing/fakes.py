# SPDX-License-Identifier: MIT
"""Offline fakes: a scripted Fetcher and a fake plugin with ranged_content and server_queue."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from shelfhold.sdk.connected import (
    ConnectedCapability,
    ServiceHealth,
    Ticket,
    TicketState,
)
from shelfhold.sdk.fetcher import CookieJarView, DownloadedFile, Response
from shelfhold.sdk.models import API_VERSION, Cursor, FilterSpec, Page, PluginKind, RateLimit
from shelfhold.sdk.refs import (
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
from shelfhold.sdk.source import Capability, ContentRemoved, SourceManifest, SubSource


class NetworkForbidden(Exception):
    """A request that the script does not contain (record_mode none)."""


class FakeFetcher:
    """Scripted Fetcher: answers only what it was given, records everything else."""

    record_mode = "none"

    def __init__(
        self,
        responses: dict[tuple[str, str], Response] | None = None,
        downloads: dict[str, bytes] | None = None,
    ) -> None:
        self._responses = dict(responses or {})
        self._downloads = dict(downloads or {})
        self.unscripted: list[str] = []

    @classmethod
    def from_cassette(cls, path: Path) -> "FakeFetcher":
        """Replay a synthetic cassette: {"interactions": [{method, url, status, body?}]}."""
        data = json.loads(path.read_text(encoding="utf-8"))
        responses = {
            (i["method"].upper(), i["url"]): Response(
                status=i["status"],
                headers=i.get("headers", {}),
                body=i.get("body", "").encode(),
            )
            for i in data["interactions"]
        }
        return cls(responses)

    def _reply(self, method: str, url: str) -> Response:
        try:
            return self._responses[(method, url)]
        except KeyError:
            self.unscripted.append(f"{method} {url}")
            raise NetworkForbidden(f"{method} {url} is not scripted (record_mode=none)") from None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
        cache_ttl: int | None = None,
    ) -> Response:
        return self._reply("GET", url)

    async def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return self._reply("POST", url)

    async def download(self, req: ImageRequest) -> DownloadedFile:
        if req.url not in self._downloads:
            self.unscripted.append(f"{req.method} {req.url}")
            raise NetworkForbidden(f"download of {req.url} is not scripted (record_mode=none)")
        content = self._downloads[req.url]
        return DownloadedFile(
            sha256=hashlib.sha256(content).hexdigest(), size=len(content), content=content
        )

    def cookies(self) -> CookieJarView:
        return CookieJarView()


FAKE_KEY = "fake-ranged"
FAKE_HEALTH_URL = "https://fake-service.invalid/health"
_SERIES_ID = "series/fake-novel"
_CHAPTERS = 10
_VOLUME_ONE = 8  # chapters 1..8 are Vol.1 (closed), 9..10 Vol.2 (rolling)


def fake_ranged_fetcher() -> FakeFetcher:
    """Fetcher scripted for FakeRangedConnectedService, with nothing else reachable."""
    return FakeFetcher(
        responses={("GET", FAKE_HEALTH_URL): Response(status=200, body=b'{"version":"1.0.0"}')}
    )


class FakeRangedConnectedService:
    """Fake plugin declaring `ranged_content` and `server_queue` together (EF-SRC-034).

    It is a minimal connected service: no categories, no release_claim. `handle_offset` stands
    for the local ids of the tool's own database, which a reinstall renumbers; the upstream
    identities (`remote_unit_id`, `SeriesRef.remote_id`) must not move.
    """

    wake_target: str | None = None

    def __init__(self, handle_offset: int = 0) -> None:
        self.handle_offset = handle_offset
        self.manifest = SourceManifest(
            key=FAKE_KEY,
            kind=PluginKind.SOURCE,
            version="1.0.0",
            api=API_VERSION,
            name="Fake ranged connected service",
            langs=["en"],
            content_kinds={"file"},
            url_patterns=[],
            capabilities=Capability.SEARCH | Capability.RANGED_CONTENT,
            rate_limit=RateLimit(requests_per_second=5.0, concurrency=2),
        )
        self.connected = ConnectedCapability.SERVER_QUEUE | ConnectedCapability.HEALTH_PROBE
        self._tickets: dict[str, tuple[UnitRef | RangeRef, int]] = {}
        self._counter = 0

    def reinstall(self) -> "FakeRangedConnectedService":
        """The same tool after a reinstall: new local ids, same upstream identities."""
        return type(self)(handle_offset=self.handle_offset + 1000)

    # --- Source -------------------------------------------------------------------------------
    def filters(self) -> list[FilterSpec]:
        return []

    async def sub_sources(self, f: Any) -> list[SubSource]:
        return []

    def _series(self) -> SeriesRef:
        return SeriesRef(source_key=FAKE_KEY, remote_id=_SERIES_ID, title="Fake Novel")

    async def search(
        self,
        f: Any,
        q: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        return Page[SeriesRef](items=[self._series()], next=None)

    async def resolve_url(self, f: Any, url: str) -> SeriesRef | UnitInfo | None:
        return None

    async def get_series(self, f: Any, ref: SeriesRef) -> SeriesInfo:
        return SeriesInfo(ref=ref, title=ref.title, authors=["Nobody"], external_ids={})

    async def list_units(self, f: Any, ref: SeriesRef) -> list[UnitInfo]:
        units = []
        for n in range(1, _CHAPTERS + 1):
            first_volume = n <= _VOLUME_ONE
            units.append(
                UnitInfo(
                    remote_unit_id=f"{FAKE_KEY}:ch-{n}",
                    source_order=n,
                    number=str(n),
                    number_sort=Decimal(n),
                    volume_label="Vol.1" if first_volume else "Vol.2",
                    volume_closed=first_volume,
                    lang="en",
                    memo={"fetch_handle": self.handle_offset + n},
                )
            )
        return units

    def _asset(self, name: str) -> FileAsset:
        return FileAsset(
            request=ImageRequest(url=f"https://fake-service.invalid/files/{name}"),
            mime="application/epub+zip",
            filename=f"{name}.epub",
        )

    async def fetch_unit(self, f: Any, ref: SeriesRef, u: UnitInfo) -> UnitPayload:
        return self._asset(u.remote_unit_id.rsplit(":", 1)[-1])

    async def _range_payload(self, target: RangeRef) -> RangedPayload:
        units = [
            u
            for u in await self.list_units(None, target.series)
            if target.first.source_order <= u.source_order <= target.last.source_order
        ]
        ids = [u.remote_unit_id for u in units]
        return RangedPayload(
            asset=self._asset(f"{ids[0].rsplit(':', 1)[-1]}-to-{ids[-1].rsplit(':', 1)[-1]}"),
            covers=ids,
            bounds={i: (f"{i}#start", f"{i}#end") for i in ids},
            volume_closed=all(u.volume_closed for u in units),
        )

    async def fetch_range(self, f: Any, target: RangeRef) -> RangedPayload:
        return await self._range_payload(target)

    def migrate_ref(self, old_source_key: str, old_remote_id: str) -> tuple[str, str] | None:
        return None

    def migrate_memo(self, version: int, memo: dict[str, Any]) -> dict[str, Any]:
        return memo

    # --- ConnectedService: SERVER_QUEUE (one ticket per target, EF-SRC-034) --------------------
    async def request(self, f: Any, target: UnitRef | RangeRef) -> Ticket:
        self._counter += 1
        token = f"{self.handle_offset}-t{self._counter}"
        self._tickets[token] = (target, 0)
        return Ticket(token=token, target_kind="range" if isinstance(target, RangeRef) else "unit")

    async def poll(self, f: Any, t: Ticket) -> TicketState:
        target, polls = self._tickets[t.token]
        if polls < 0:
            return "gone"
        self._tickets[t.token] = (target, polls + 1)
        return ("queued", "downloading", "ready")[min(polls, 2)]

    async def fetch(self, f: Any, t: Ticket) -> UnitPayload | RangedPayload:
        target, polls = self._tickets[t.token]
        if polls == -1:
            raise ContentRemoved("ticket cancelled")
        if isinstance(target, RangeRef):
            return await self._range_payload(target)
        return self._asset(target.unit.remote_unit_id.rsplit(":", 1)[-1])

    async def cancel(self, f: Any, t: Ticket) -> None:
        target, _ = self._tickets[t.token]
        self._tickets[t.token] = (target, -1)

    # --- ConnectedService: HEALTH_PROBE --------------------------------------------------------
    async def semantic_health(self, f: Any) -> ServiceHealth:
        response = await f.get(FAKE_HEALTH_URL)
        return ServiceHealth(
            ok=response.status == 200, version=json.loads(response.body)["version"]
        )

    async def request_refresh(self, f: Any, refs: list[SeriesRef]) -> None:
        return None
