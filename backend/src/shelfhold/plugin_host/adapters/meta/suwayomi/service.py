"""Suwayomi as a connected service (EF-SRC-008, 021, 031, 033, 036).

CBZ only: a chapter is one archive from `GET /api/v1/chapter/{id}/download`, never pages one by
one. GraphQL goes out as JSON and nothing else. Identities are upstream: a series is
`(suwayomi:<sourceId>, manga.url)` and a chapter `suwayomi:<sourceId>:<chapter.url>`; the integer
ids of Suwayomi's own database are only cached `fetch_handle`s, re-resolved when they stop matching.
"""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from shelfhold.catalog.lang import LanguageError, normalize_lang
from shelfhold.plugin_host.adapters.meta.suwayomi import queries as q
from shelfhold.plugin_host.adapters.meta.suwayomi.versions import is_supported
from shelfhold.plugin_host.errors import AuthModeUnsupported, ServiceVersionMismatch
from shelfhold.sdk.connected import (
    ConnectedCapability,
    ServiceHealth,
    Ticket,
    TicketState,
)
from shelfhold.sdk.fetcher import Fetcher, Response
from shelfhold.sdk.lang import LanguageTag
from shelfhold.sdk.models import (
    API_VERSION,
    AuthBasic,
    Cursor,
    FilterSpec,
    Page,
    PluginKind,
    RateLimit,
)
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
from shelfhold.sdk.source import (
    AuthRequired,
    Capability,
    ContentRemoved,
    RateLimited,
    SourceChanged,
    SourceManifest,
    SubSource,
    TemporarilyUnavailable,
)

KEY = "suwayomi"
CBZ_MIME = "application/vnd.comicbook+zip"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def build_manifest() -> SourceManifest:
    """Same content as `sources/meta_suwayomi/manifest.json` (a test keeps the two equal)."""
    return SourceManifest(
        key=KEY,
        kind=PluginKind.SOURCE,
        version="0.1.0",
        api=API_VERSION,
        name="Suwayomi (connected service)",
        settings_schema={
            "type": "object",
            "required": ["base_url"],
            "properties": {
                "base_url": {"type": "string", "title": "Suwayomi address"},
                "username": {"type": "string"},
                "password": {"type": "string", "secret": True},
            },
        },
        content_warning="mixed",
        respects_robots=True,
        homepage="https://github.com/Suwayomi/Suwayomi-Server",
        langs=[],
        content_kinds={"file"},
        url_patterns=[],
        capabilities=Capability.FOLLOW | Capability.SEARCH | Capability.SUB_SOURCES,
        rate_limit=RateLimit(requests_per_second=5.0, concurrency=2),
        auth=[AuthBasic()],
        content_trust="untrusted",
    )


class SuwayomiService:
    wake_target: str | None = None

    def __init__(
        self, base_url: str, *, refresh_mode: Literal["always_on", "driven"] = "always_on"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.refresh_mode = refresh_mode
        self.manifest = build_manifest()
        self.connected = (
            ConnectedCapability.CATEGORIES
            | ConnectedCapability.SERVER_QUEUE
            | ConnectedCapability.HEALTH_PROBE
            | ConnectedCapability.AUTH_MODE
        )
        self._mode_verified = False
        self._handles: dict[str, int] = {}  # ticket token -> last known local chapter id

    # -- transport ---------------------------------------------------------------------------
    async def _post(self, f: Fetcher, query: str, variables: dict[str, Any] | None = None) -> Any:
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables
        response = await f.post(
            self.base_url + q.GRAPHQL_PATH,
            json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        self._raise_for_status(response)
        try:
            payload = json.loads(response.body)
        except ValueError as exc:
            raise SourceChanged("the server did not answer with JSON") from exc
        if not isinstance(payload, dict):
            raise SourceChanged("unexpected GraphQL envelope")
        return payload

    @staticmethod
    def _raise_for_status(response: Response) -> None:
        status = response.status
        if status == 401:
            if b"csrf" in response.body.lower():
                raise ServiceVersionMismatch("CSRF mismatch: not an authentication problem")
            raise AuthRequired("the server refused the credentials")
        if status == 429:
            raise RateLimited("rate limited by the server")
        if status >= 500:
            raise TemporarilyUnavailable(f"the server answered {status}")
        if status >= 400:
            raise SourceChanged(f"unexpected answer {status}")

    @staticmethod
    def _unauthorized(payload: dict[str, Any]) -> bool:
        errors = payload.get("errors") or []
        return any("unauthorized" in str(e.get("message", "")).lower() for e in errors)

    async def _query(
        self, f: Fetcher, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """A data operation: refused first if the tool runs in a mode this release cannot drive."""
        await self._ensure_supported_mode(f)
        payload = await self._post(f, query, variables)
        if self._unauthorized(payload):
            raise AuthRequired("the server refused the credentials")
        if payload.get("errors") or not isinstance(payload.get("data"), dict):
            raise SourceChanged("GraphQL error: " + str(payload.get("errors"))[:200])
        data: dict[str, Any] = payload["data"]
        return data

    # -- EF-SRC-031: authentication mode -----------------------------------------------------
    async def auth_mode(self, f: Fetcher) -> str:
        """`basic_auth`, `ui_login` or `none`: the `authMode` the server reports comes first.

        Fallback, not verified by S5 for this very query: a 200 answer carrying an
        `Unauthorized` error means a token-only mode, hence `ui_login`.
        """
        payload = await self._post(f, q.AUTH_MODE)
        data = payload.get("data")
        reported = ((data or {}).get("settings") or {}).get("authMode")
        if isinstance(reported, str) and reported:
            return reported.lower()
        if self._unauthorized(payload):
            return "ui_login"
        raise SourceChanged("the server did not report its authentication mode")

    async def _ensure_supported_mode(self, f: Fetcher) -> None:
        if self._mode_verified:
            return
        if await self.auth_mode(f) == "ui_login":
            raise AuthModeUnsupported()
        self._mode_verified = True

    # -- HEALTH_PROBE ------------------------------------------------------------------------
    async def semantic_health(self, f: Fetcher) -> ServiceHealth:
        about = (await self._post(f, q.ABOUT)).get("data", {}).get("aboutServer") or {}
        version = str(about.get("version", ""))
        if not is_supported(version):
            return ServiceHealth(ok=False, version=version, detail="source.version_unsupported")
        if await self.auth_mode(f) == "ui_login":
            return ServiceHealth(ok=False, version=version, detail="source.auth_mode_unsupported")
        return ServiceHealth(ok=True, version=version)

    async def request_refresh(self, f: Fetcher, refs: list[SeriesRef]) -> None:
        """Always-on: Suwayomi follows its own schedule and nothing is sent. The driven mode
        belongs to M1b (EF-SRC-025)."""
        return None

    # -- sub-sources and limits (EF-SRC-021) -------------------------------------------------
    def filters(self) -> list[FilterSpec]:
        return []

    async def sub_sources(self, f: Fetcher) -> list[SubSource]:
        data = await self._query(f, q.SOURCES)
        subs = []
        for node in data["sources"]["nodes"]:
            key = f"{KEY}:{node['id']}"
            subs.append(
                SubSource(
                    source_key=key,
                    name=str(node["displayName"]),
                    lang=_lang(str(node.get("lang", ""))),
                    content_warning="nsfw" if node.get("isNsfw") else "safe",
                    limit_key=key,
                )
            )
        return subs

    @staticmethod
    def limit_key_for(ref: SeriesRef) -> str:
        """The rate-limit key of the upstream source a series comes from."""
        return ref.source_key

    def limit_slots(self, subs: list[SubSource]) -> dict[str, int]:
        return {s.limit_key: self.manifest.rate_limit.concurrency for s in subs}

    # -- series ------------------------------------------------------------------------------
    def _ref(self, node: dict[str, Any]) -> SeriesRef:
        return SeriesRef(
            source_key=f"{KEY}:{node['sourceId']}",
            remote_id=str(node["url"]),
            title=str(node["title"]),
            cover=ImageRequest(url=self.base_url + q.THUMBNAIL_PATH.format(id=node["id"])),
            memo={"fetch_handle": node["id"]},
        )

    async def search(
        self,
        f: Fetcher,
        query: str,
        filters: dict[str, Any],
        cursor: Cursor | None,
        *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:
        """Series of the Suwayomi library, optionally of one sub-source and matching `query`."""
        data = await self._query(f, q.LIBRARY)
        wanted = query.casefold()
        refs = [
            self._ref(n)
            for n in data["mangas"]["nodes"]
            if wanted in str(n["title"]).casefold()
            and (sub is None or f"{KEY}:{n['sourceId']}" == sub)
        ]
        return Page[SeriesRef](items=refs, next=None)

    async def resolve_url(self, f: Fetcher, url: str) -> SeriesRef | UnitInfo | None:
        return None

    async def _manga(self, f: Fetcher, ref: SeriesRef) -> dict[str, Any]:
        """The manga of a reference, found by `(sourceId, manga.url)`, never by its local id."""
        data = await self._query(f, q.ALL_MANGAS)
        for node in data["mangas"]["nodes"]:
            if f"{KEY}:{node['sourceId']}" == ref.source_key and str(node["url"]) == ref.remote_id:
                found: dict[str, Any] = node
                return found
        raise ContentRemoved(f"{ref.remote_id} is no longer known to the server")

    async def get_series(self, f: Fetcher, ref: SeriesRef) -> SeriesInfo:
        node = await self._manga(f, ref)
        authors = [str(a) for a in (node.get("author"), node.get("artist")) if a]
        return SeriesInfo(
            ref=self._ref(node),
            title=str(node["title"]),
            description=node.get("description") or None,
            authors=list(dict.fromkeys(authors)),
        )

    async def followed_series(self, f: Fetcher) -> list[SeriesRef]:
        return list((await self.search(f, "", {}, None)).items)

    async def set_follow(self, f: Fetcher, ref: SeriesRef, on: bool) -> None:
        node = await self._manga(f, ref)
        await self._query(f, q.SET_LIBRARY, {"id": node["id"], "inLibrary": on})

    # -- units -------------------------------------------------------------------------------
    @staticmethod
    def _unit_id(source_key: str, chapter_url: str) -> str:
        return f"{source_key}:{chapter_url}"

    async def _chapters(self, f: Fetcher, ref: SeriesRef) -> list[dict[str, Any]]:
        manga = await self._manga(f, ref)
        data = await self._query(f, q.CHAPTERS, {"mangaId": manga["id"]})
        nodes: list[dict[str, Any]] = data["chapters"]["nodes"]
        return nodes

    async def list_units(self, f: Fetcher, ref: SeriesRef) -> list[UnitInfo]:
        units = [self._unit(ref, c) for c in await self._chapters(f, ref)]
        return sorted(units, key=lambda u: u.source_order)

    def _unit(self, ref: SeriesRef, chapter: dict[str, Any]) -> UnitInfo:
        number = chapter.get("chapterNumber")
        number_sort: Decimal | None = None
        if isinstance(number, int | float) and number >= 0:
            try:
                number_sort = Decimal(str(number)).normalize()
            except InvalidOperation:
                number_sort = None
        return UnitInfo(
            remote_unit_id=self._unit_id(ref.source_key, str(chapter["url"])),
            source_order=int(chapter["sourceOrder"]),
            number=format(number_sort, "f") if number_sort is not None else None,
            number_sort=number_sort,
            title=chapter.get("name") or None,
            team=chapter.get("scanlator") or None,
            published_at=_upload_date(chapter.get("uploadDate")),
            memo={"fetch_handle": chapter["id"], "chapter_url": chapter["url"]},
        )

    async def _current_handle(
        self, f: Fetcher, ref: SeriesRef, chapter_url: str, hint: int | None
    ) -> int:
        """The local chapter id now: the cached handle if it still names this chapter, else
        re-resolved by `(sourceId, chapter.url)` (after a reinstall or a restore, EF-SRC-036)."""
        if hint is not None:
            node = (await self._query(f, q.CHAPTER, {"id": hint}))["chapter"]
            if node and node.get("url") == chapter_url:
                return hint
        for chapter in await self._chapters(f, ref):
            if str(chapter["url"]) == chapter_url:
                return int(chapter["id"])
        raise ContentRemoved(f"{chapter_url} is no longer known to the server")

    def _asset(self, handle: int, chapter_url: str) -> FileAsset:
        stem = _UNSAFE.sub("_", chapter_url.rsplit("/", 1)[-1]) or "chapter"
        stem = stem.removesuffix(".cbz")
        return FileAsset(
            request=ImageRequest(url=self.base_url + q.DOWNLOAD_PATH.format(id=handle)),
            mime=CBZ_MIME,
            filename=f"{stem}.cbz",
        )

    async def fetch_unit(self, f: Fetcher, ref: SeriesRef, u: UnitInfo) -> UnitPayload:
        chapter_url = _chapter_url(u)
        hint = u.memo.get("fetch_handle")
        handle = await self._current_handle(
            f, ref, chapter_url, hint if isinstance(hint, int) else None
        )
        return self._asset(handle, chapter_url)

    async def resolve_page(self, f: Fetcher, u: Any, p: Any) -> ImageRequest:
        raise NotImplementedError("Suwayomi is acquired as CBZ, never page by page (EF-SRC-008)")

    def migrate_ref(self, old_source_key: str, old_remote_id: str) -> tuple[str, str] | None:
        return None

    def migrate_memo(self, version: int, memo: dict[str, Any]) -> dict[str, Any]:
        return memo

    # -- SERVER_QUEUE ------------------------------------------------------------------------
    async def request(self, f: Fetcher, target: UnitRef | RangeRef) -> Ticket:
        if isinstance(target, RangeRef):
            raise ValueError("Suwayomi has no ranged content: one ticket per chapter")
        chapter_url = _chapter_url(target.unit)
        handle = await self._current_handle(f, target.series, chapter_url, None)
        await self._query(f, q.ENQUEUE, {"id": handle})
        token = json.dumps(
            {"s": target.series.source_key, "m": target.series.remote_id, "c": chapter_url},
            separators=(",", ":"),
        )
        self._handles[token] = handle
        return Ticket(token=token, target_kind="unit")

    @staticmethod
    def _decode(t: Ticket) -> tuple[SeriesRef, str]:
        raw = json.loads(t.token)
        return SeriesRef(source_key=raw["s"], remote_id=raw["m"], title=""), str(raw["c"])

    async def _ticket_node(self, f: Fetcher, t: Ticket) -> tuple[int, dict[str, Any]] | None:
        ref, chapter_url = self._decode(t)
        hint = self._handles.get(t.token)
        if hint is not None:  # one query in the common case: the cached handle still matches
            node = (await self._query(f, q.CHAPTER, {"id": hint}))["chapter"]
            if node and node.get("url") == chapter_url:
                return hint, node
        try:
            handle = await self._current_handle(f, ref, chapter_url, None)
        except ContentRemoved:
            return None
        self._handles[t.token] = handle
        node = (await self._query(f, q.CHAPTER, {"id": handle}))["chapter"]
        return (handle, node) if node else None

    async def poll(self, f: Fetcher, t: Ticket) -> TicketState:
        found = await self._ticket_node(f, t)
        if found is None:
            return "gone"
        return "ready" if found[1].get("isDownloaded") else "downloading"

    async def fetch(self, f: Fetcher, t: Ticket) -> UnitPayload | RangedPayload:
        found = await self._ticket_node(f, t)
        if found is None:
            raise ContentRemoved("the chapter is gone from the server")
        if not found[1].get("isDownloaded"):
            raise TemporarilyUnavailable("the chapter is not downloaded yet")
        return self._asset(found[0], self._decode(t)[1])

    async def cancel(self, f: Fetcher, t: Ticket) -> None:
        found = await self._ticket_node(f, t)
        if found is not None:
            await self._query(f, q.DEQUEUE, {"id": found[0]})


def _chapter_url(unit: UnitInfo) -> str:
    memo_url = unit.memo.get("chapter_url")
    if isinstance(memo_url, str):
        return memo_url
    _, _, rest = unit.remote_unit_id.partition(":")
    return rest.split(":", 1)[1] if ":" in rest else rest


def _lang(raw: str) -> LanguageTag | Literal["mul"]:
    if raw.casefold() in ("all", "multi", ""):
        return "mul"
    try:
        return normalize_lang(raw).tag
    except LanguageError:
        return "mul"


def _upload_date(raw: Any) -> datetime | None:
    try:
        millis = int(raw)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, UTC) if millis > 0 else None
