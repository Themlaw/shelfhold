"""lncrawl as a connected service (EF-SRC-010, 032, 033, 034; EF-BIB-049, 051).

The user's own instance, over its REST API (`/api/v1`, pinned in `docs/admin/lncrawl.md`). A job
builds one EPUB for a range of chapters; the EPUB is the file of a volume (`ranged_content`). What
Shelfhold sees of the instance is opaque: the id of a novel, of a chapter, of a job, of an
artifact. It does not name, list, pick or switch a crawler, and nothing the instance says about
itself reaches a reference, an error or a log.

The two steps of a request are bound to what was announced. The ticket carries the novel and the
first and last chapter asked for; the second step, the file, is built from the artifact identifier
of the answer on the host of the instance and nowhere else, and only when the answer is about that
novel, that range and chapters inside it. The host checks the same again before it receives a byte
(`acquisition/application/lncrawl_ranges.py`).
"""

import asyncio
import hashlib
import json
import re
from decimal import Decimal
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from shelfhold.catalog.lang import LanguageError, normalize_lang
from shelfhold.plugin_host.adapters.meta.lncrawl import api
from shelfhold.plugin_host.errors import AuthExpired
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
    AuthApiKey,
    Cursor,
    DomainRule,
    FilterSpec,
    Page,
    Permissions,
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
    TemporarilyUnavailable,
)

KEY: Final = "lncrawl"
EPUB_MIME: Final = "application/epub+zip"
_PLACEHOLDER_HOST: Final = "lncrawl.invalid"  # replaced by the host of the admin's `base_url`
_STATES: Final[dict[str, TicketState]] = {
    "queued": "queued",
    "running": "downloading",
    "done": "ready",
    "failed": "failed",
    "cancelled": "gone",
}


def build_manifest() -> SourceManifest:
    """Same content as `sources/meta_lncrawl/manifest.json` (a test keeps the two equal)."""
    return SourceManifest(
        key=KEY,
        kind=PluginKind.SOURCE,
        version="0.1.0",
        api=API_VERSION,
        name="lncrawl (connected service)",
        permissions=Permissions(
            domains=[
                DomainRule(pattern=_PLACEHOLDER_HOST, purpose="api", base_url_setting="base_url")
            ]
        ),
        settings_schema={
            "type": "object",
            "required": ["base_url"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "format": "uri",
                    "title": "Address of your instance",
                },
                "api_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                    "title": "API key of your account",
                },
            },
        },
        content_warning="mixed",
        terms_url="https://github.com/lncrawl/lightnovel-crawler/blob/dev/LICENSE",
        respects_robots=False,  # the user's own tool, not a site
        homepage="https://github.com/lncrawl/lightnovel-crawler",
        langs=[],
        content_kinds={"file"},
        url_patterns=[],
        capabilities=Capability.SEARCH | Capability.FOLLOW | Capability.RANGED_CONTENT,
        rate_limit=RateLimit(requests_per_second=5.0, concurrency=1),
        auth=[AuthApiKey(location="header", name="X-API-Key")],
        translation_nature="community",  # chapters relayed by fan teams (EF-BIB-049)
        content_trust="untrusted",  # the EPUB is kept as it came, only its derivative leaves
    )


class LncrawlService:
    wake_target: str | None = None

    def __init__(
        self, base_url: str = "", *, poll_delay: float = 2.0, poll_limit: int = 40
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.manifest = build_manifest()
        self.connected = (
            ConnectedCapability.SERVER_QUEUE
            | ConnectedCapability.HEALTH_PROBE
            | ConnectedCapability.INSTANCE_FINGERPRINT
        )
        self._names: set[str] = set()  # what the instance says of itself: never shown (below)
        self._poll_delay = poll_delay
        self._poll_limit = poll_limit

    def __repr__(self) -> str:
        return "LncrawlService()"

    @property
    def origin(self) -> str | None:
        """`scheme://host[:port]` of the instance: the only origin a file may come from."""
        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        return f"{parts.scheme}://{parts.netloc}"

    # -- transport ---------------------------------------------------------------------------
    def _url(self, path: str) -> str:
        if self.origin is None:
            raise TemporarilyUnavailable("no instance is configured")
        return self.base_url + api.API + path

    @staticmethod
    def _raise_for_status(response: Response, f: Fetcher) -> None:
        """Fixed messages only: the body of a refusal is never copied anywhere."""
        status = response.status
        if status in (401, 403):
            if getattr(f, "has_credentials", False):
                raise AuthExpired("the instance refused the credentials")
            raise AuthRequired("the instance requires credentials")
        if status == 404:
            raise ContentRemoved("the instance does not know it")
        if status == 429:
            raise RateLimited("rate limited by the instance")
        if status >= 500:
            raise TemporarilyUnavailable("the instance answered an error")
        if status >= 400:
            raise SourceChanged("the instance refused the request")

    async def _get(
        self, f: Fetcher, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await f.get(self._url(path), params=params)
        self._raise_for_status(response, f)
        return self._learned(api.mapping(api.loads(response)))

    def _learned(self, node: dict[str, Any]) -> dict[str, Any]:
        """The crawler or source name an instance volunteers is remembered, so that it is cut
        from every title, team and author the instance sends after or beside it."""
        stack: list[Any] = [node]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in api.SELF_NAMING and isinstance(value, str) and value.strip():
                        self._names.add(value.strip())
                    elif isinstance(value, dict | list):
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
        return node

    def _shown(self, value: object, *, limit: int = 200) -> str | None:
        """A display string without any name the instance gave of itself."""
        shown = api.text(value, limit=limit)
        for name in self._names:
            if shown is not None:
                shown = re.sub(re.escape(name), "", shown, flags=re.IGNORECASE)
                shown = " ".join(shown.split()) or None
        return shown

    async def _post(self, f: Fetcher, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await f.post(
            self._url(path), json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )  # fmt: skip
        self._raise_for_status(response, f)
        return api.mapping(api.loads(response))

    async def _pages(
        self, f: Fetcher, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Every entry of a listing, bounded in pages and in entries; a cursor that comes back is
        a loop and is refused."""
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(api.MAX_PAGES):
            query = dict(params or {})
            if cursor is not None:
                query["cursor"] = cursor
            entries, cursor = api.items(await self._get(f, path, query or None))
            if len(found) + len(entries) > api.MAX_ITEMS:
                break  # before the memory is spent
            found.extend(api.slim(e) for e in entries)  # only what is read, bounded in size
            if cursor is None:
                return found
            if cursor in seen:
                break
            seen.add(cursor)
        raise SourceChanged("the instance sent a listing without end")

    # -- identities --------------------------------------------------------------------------
    @staticmethod
    def _unit_id(novel: str, chapter: str) -> str:
        return f"{KEY}:{novel}:{chapter}"

    @staticmethod
    def _chapter_of(novel: str, remote_unit_id: str) -> str:
        prefix = f"{KEY}:{novel}:"
        if not remote_unit_id.startswith(prefix):
            raise SourceChanged("a chapter of another novel cannot be asked for here")
        return api.identifier(remote_unit_id[len(prefix) :])

    def _ref(self, entry: dict[str, Any]) -> SeriesRef:
        return SeriesRef(
            source_key=KEY, remote_id=api.identifier(entry.get("id")),
            title=self._shown(entry.get("title"), limit=300) or "",
        )  # fmt: skip

    # -- Source ------------------------------------------------------------------------------
    def filters(self) -> list[FilterSpec]:
        return []

    async def sub_sources(self, f: Fetcher) -> list[Any]:
        return []

    async def search(
        self, f: Fetcher, q: str, filters: dict[str, Any], cursor: Cursor | None,
        *, sub: str | None = None,
    ) -> Page[SeriesRef]:  # fmt: skip
        """The library of the account on its own instance, filtered by title: Shelfhold asks the
        instance for nothing it did not already hold, and no crawler is searched or named."""
        wanted = q.strip().casefold()
        refs = [self._ref(e) for e in await self._pages(f, api.LIBRARY)]
        return Page[SeriesRef](
            items=[r for r in refs if not wanted or wanted in r.title.casefold()], next=None
        )

    async def followed_series(self, f: Fetcher) -> list[SeriesRef]:
        return list((await self.search(f, "", {}, None)).items)

    async def resolve_url(self, f: Fetcher, url: str) -> SeriesRef | UnitInfo | None:
        return None  # a novel is named by the instance's own library, never by a pasted address

    async def _novel(self, f: Fetcher, ref: SeriesRef) -> dict[str, Any]:
        return await self._get(f, api.NOVEL.format(novel=api.identifier(ref.remote_id)))

    async def get_series(self, f: Fetcher, ref: SeriesRef) -> SeriesInfo:
        node = await self._novel(f, ref)
        authors = node.get("authors")
        names = [a for a in (self._shown(x, limit=200) for x in authors) if a] if isinstance(
            authors, list
        ) else []  # fmt: skip
        return SeriesInfo(
            ref=ref.model_copy(update={"title": self._shown(node.get("title"), limit=300) or ""}),
            title=self._shown(node.get("title"), limit=300) or ref.title,
            description=api.text(node.get("synopsis"), limit=4000),
            authors=names[:20],
        )

    @staticmethod
    def _lang(raw: object) -> tuple[LanguageTag | None, str | None]:
        value = api.text(raw, limit=35)
        if value is None:
            return None, None
        try:
            return normalize_lang(value).tag, value
        except LanguageError:
            return None, value

    async def list_units(self, f: Fetcher, ref: SeriesRef) -> list[UnitInfo]:
        novel = api.identifier(ref.remote_id)
        lang, lang_raw = self._lang((await self._novel(f, ref)).get("language"))
        units: list[UnitInfo] = []
        seen: set[str] = set()
        orders: set[int] = set()
        for entry in await self._pages(f, api.CHAPTERS.format(novel=novel)):
            chapter = api.identifier(entry.get("id"))
            order = api.number(entry.get("order"))
            if order is None or chapter in seen or order in orders:
                raise SourceChanged("the instance listed a chapter twice")
            seen.add(chapter)
            orders.add(order)
            closed = entry.get("closed")
            label = api.volume_label(entry.get("volume"))
            units.append(
                UnitInfo(
                    remote_unit_id=self._unit_id(novel, chapter), source_order=order,
                    number=str(order), number_sort=Decimal(order), kind="chapter",
                    title=self._shown(entry.get("title"), limit=300), volume_label=label,
                    volume_closed=closed if label and isinstance(closed, bool) else None,
                    team=api.team(self._shown(entry.get("team"))), lang=lang, lang_raw=lang_raw,
                )
            )  # fmt: skip
        return sorted(units, key=lambda u: u.source_order)

    async def fetch_unit(self, f: Fetcher, ref: SeriesRef, u: UnitInfo) -> UnitPayload:
        payload = await self._drive(f, UnitRef(series=ref, unit=u))
        if not isinstance(payload, FileAsset):
            raise SourceChanged("a chapter was asked for and a range came back")
        return payload

    async def fetch_range(self, f: Fetcher, target: RangeRef) -> RangedPayload:
        payload = await self._drive(f, target)
        if not isinstance(payload, RangedPayload):
            raise SourceChanged("a range was asked for and a chapter came back")
        return payload

    async def resolve_page(self, f: Fetcher, u: Any, p: Any) -> ImageRequest:
        raise NotImplementedError("an EPUB is a file; there are no pages to resolve")

    def migrate_ref(self, old_source_key: str, old_remote_id: str) -> tuple[str, str] | None:
        return None

    def migrate_memo(self, version: int, memo: dict[str, Any]) -> dict[str, Any]:
        return memo

    # -- SERVER_QUEUE: step one asks, step two receives what was announced -----------------------
    async def request(self, f: Fetcher, target: UnitRef | RangeRef) -> Ticket:
        kind: Literal["unit", "range"]
        if isinstance(target, RangeRef):
            kind, first, last = "range", target.first, target.last
        else:
            kind, first, last = "unit", target.unit, target.unit
        if first.source_order > last.source_order:
            raise SourceChanged("a range does not end before it starts")
        novel = api.identifier(target.series.remote_id)
        first_id, last_id = self._chapter_of(novel, first.remote_unit_id), self._chapter_of(
            novel, last.remote_unit_id
        )  # fmt: skip
        answer = await self._post(f, api.JOBS, {"novel": novel, "first": first_id, "last": last_id})
        token = json.dumps(
            {"j": api.identifier(answer.get("job")), "n": novel, "f": first_id, "l": last_id,
             "k": kind},
            separators=(",", ":"),
        )  # fmt: skip
        return Ticket(token=token, target_kind=kind)

    @staticmethod
    def _announced(t: Ticket) -> dict[str, str]:
        """What step one announced, read back from the ticket and checked again: a ticket the host
        stored is still data (it can be edited behind the facade)."""
        try:
            raw = json.loads(t.token)
        except (ValueError, RecursionError):
            raise SourceChanged("the ticket is unreadable") from None
        if not isinstance(raw, dict):
            raise SourceChanged("the ticket is unreadable")
        announced = {k: api.identifier(raw.get(k)) for k in ("j", "n", "f", "l")}
        if raw.get("k") != t.target_kind or t.target_kind not in ("unit", "range"):
            raise SourceChanged("the ticket is unreadable")
        return announced

    async def _job(self, f: Fetcher, t: Ticket) -> tuple[dict[str, str], dict[str, Any] | None]:
        announced = self._announced(t)
        try:
            answer = await self._get(f, api.JOB.format(job=announced["j"]))
        except ContentRemoved:
            return announced, None  # the instance forgot it: the host asks again
        state = answer.get("state")
        if not isinstance(state, str) or state not in api.JOB_STATES:
            raise SourceChanged("the instance sent an unknown job state")
        novel = answer.get("novel")
        if novel != announced["n"]:  # present and the one asked for: ids are local to a novel
            raise SourceChanged("the job is about another novel than the one asked for")
        return announced, answer

    async def poll(self, f: Fetcher, t: Ticket) -> TicketState:
        _, answer = await self._job(f, t)
        return "gone" if answer is None else _STATES[str(answer["state"])]

    async def fetch(self, f: Fetcher, t: Ticket) -> UnitPayload | RangedPayload:
        announced, answer = await self._job(f, t)
        if answer is None:
            raise ContentRemoved("the job is gone from the instance")
        if answer["state"] != "done":
            raise TemporarilyUnavailable("the job is not done yet")
        novel, first, last = announced["n"], announced["f"], announced["l"]
        if answer.get("first") != first or answer.get("last") != last:
            raise SourceChanged("the file is for another range than the one asked for")
        covered = api.covers(answer.get("covers"))
        await self._check_inside(f, novel, first, last, covered)
        # the file is where the instance keeps its artifacts, on its host, and nowhere else: the
        # identifier is all that is taken from the answer, never an address
        artifact = api.identifier(answer.get("artifact"))
        asset = FileAsset(
            request=ImageRequest(
                url=self.base_url + api.API + api.ARTIFACT.format(artifact=artifact)
            ),
            mime=EPUB_MIME, filename=f"{novel}-{first}-{last}.epub",
        )  # fmt: skip
        if t.target_kind == "unit":
            if covered != [first] or first != last:
                raise SourceChanged("a chapter was asked for and a range came back")
            return asset
        closed = answer.get("closed")
        return RangedPayload(
            asset=asset, covers=[self._unit_id(novel, c) for c in covered],
            volume_closed=closed if isinstance(closed, bool) else False,
        )  # fmt: skip

    async def _check_inside(
        self, f: Fetcher, novel: str, first: str, last: str, covered: list[str]
    ) -> None:
        """The chapters of the file are chapters of this novel, in order, once each, between the
        first and the last that were asked for (the order the instance gives them)."""
        order = {
            api.identifier(e.get("id")): api.number(e.get("order"))
            for e in await self._pages(f, api.CHAPTERS.format(novel=novel))
        }
        low, high = order.get(first), order.get(last)
        if low is None or high is None or low > high:
            raise SourceChanged("the range that was asked for is not in the novel")
        previous = low - 1
        for chapter in covered:
            position = order.get(chapter)
            if position is None or not previous < position <= high:
                raise SourceChanged("the file carries chapters outside the range asked for")
            previous = position

    async def cancel(self, f: Fetcher, t: Ticket) -> None:
        announced = self._announced(t)
        try:
            await self._post(f, api.CANCEL.format(job=announced["j"]), {})
        except ContentRemoved:
            return None  # already gone: that is what was wanted

    async def _drive(self, f: Fetcher, target: UnitRef | RangeRef) -> UnitPayload | RangedPayload:
        """The synchronous case of the same two steps: ask, wait a bounded time, receive."""
        ticket = await self.request(f, target)
        for _ in range(self._poll_limit):
            state = await self.poll(f, ticket)
            if state == "ready":
                return await self.fetch(f, ticket)
            if state in ("failed", "gone"):
                raise TemporarilyUnavailable("the instance did not build the file")
            if self._poll_delay:
                await asyncio.sleep(self._poll_delay)
        await self.cancel(f, ticket)  # a queue that does not end is not waited for forever
        raise TemporarilyUnavailable("the instance did not finish in time")

    # -- HEALTH_PROBE and INSTANCE_FINGERPRINT ---------------------------------------------------
    async def semantic_health(self, f: Fetcher) -> ServiceHealth:
        status = await self._get(f, api.STATUS)
        version = api.text(status.get("version"), limit=40) or ""
        if not api.major_supported(version):
            return ServiceHealth(ok=False, version=version, detail="source.version_unsupported")
        return ServiceHealth(ok=True, version=version)

    async def unloaded_sources(self, f: Fetcher, source_keys: list[str]) -> list[str]:
        return []  # Shelfhold sees no crawler: there is no upstream source to find missing

    async def download_queue_size(self, f: Fetcher) -> int:
        return -1  # the instance's queue is not Shelfhold's to read: never taken for empty

    async def blocking_update_filters(self, f: Fetcher) -> list[str]:
        return []

    async def instance_fingerprint(self, f: Fetcher) -> str:
        """A hash of the identifier the instance gives itself: what tells a reinstalled instance
        from the same one, without keeping the identifier."""
        identity = api.mapping(await self._get(f, api.STATUS)).get("instance")
        if not isinstance(identity, str) or not identity:
            return ""
        return hashlib.sha256(f"{KEY}:{api.identifier(identity)}".encode()).hexdigest()
