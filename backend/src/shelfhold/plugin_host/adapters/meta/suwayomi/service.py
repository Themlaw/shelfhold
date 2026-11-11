"""Suwayomi as a connected service (EF-SRC-008, 021, 031, 033, 036).

CBZ only: a chapter is one archive from `GET /api/v1/chapter/{id}/download`, never pages one by
one. GraphQL goes out as JSON and nothing else. Identities are upstream: a series is
`(suwayomi:<sourceId>, manga.url)` and a chapter `suwayomi:<sourceId>:<chapter.url>`; the integer
ids of Suwayomi's own database are only cached `fetch_handle`s, re-resolved when they stop matching.
"""

import hashlib
import json
import math
import re
import time
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Literal

from shelfhold.catalog.lang import LanguageError, normalize_lang
from shelfhold.infrastructure.clock import DEFAULT_SKEW
from shelfhold.plugin_host.adapters.meta.suwayomi import queries as q
from shelfhold.plugin_host.adapters.meta.suwayomi.versions import is_supported
from shelfhold.plugin_host.errors import (
    AuthExpired,
    AuthModeUnsupported,
    ServiceVersionMismatch,
)
from shelfhold.sdk.connected import (
    ConnectedCapability,
    ReleaseResult,
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
SUPPORTED_MODES = frozenset({"none", "basic_auth", "ui_login"})  # every other mode is refused
# D1: a role that lets an account install extensions. The service account never has one.
_PRIVILEGED_WORDS = ("ADMIN", "SUPERUSER", "SUPER_USER", "OWNER", "ROOT", "EXTENSION")
_ROLES_UNREADABLE = (
    "les rôles du compte de service n'ont pas pu être lus de façon sûre : "
    "le service attend, en pause, jusqu'à un compte aux droits minimaux"
)
_UNKNOWN_FIELD = re.compile(
    r"(cannot query field|fieldundefined|is undefined|unknown field|not defined)", re.IGNORECASE
)
_ROLE_KEYS = ("name", "role", "value", "id", "type")
MARK = "Shelfhold"  # the category that holds a series out of updates (= EXCLUSION_MARK)
UPDATE_STRATEGIES = frozenset({"ALWAYS_UPDATE", "ONLY_FETCH_ONCE"})
PROBE_VALID_SECONDS = 600.0  # a follow needs a fingerprint read this lately (one cycle)
MANGA_CACHE_SECONDS = 60.0  # one follow cycle; `begin_cycle` drops it earlier
MAX_FILENAME_BYTES = 120
_FORBIDDEN_NAME = re.compile(r'[\\/:*?"<>|]')


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
        terms_url="https://github.com/Suwayomi/Suwayomi-Server/blob/master/LICENSE",
        respects_robots=True,
        homepage="https://github.com/Suwayomi/Suwayomi-Server",
        langs=[],
        content_kinds={"file"},
        url_patterns=[],
        capabilities=Capability.FOLLOW | Capability.SEARCH | Capability.SUB_SOURCES,
        rate_limit=RateLimit(requests_per_second=5.0, concurrency=2),
        auth=[AuthBasic()],
        translation_nature="community",  # scanlation relayed by fan teams (EF-BIB-049)
        content_trust="untrusted",
    )


_VOLUME_PREFIX = re.compile(r"^\s*vol(?:ume)?\.?\s*(\d{1,4}(?:\.\d{1,2})?)\b", re.IGNORECASE)


def _volume_label(name: object) -> str | None:
    """`Vol.3 Ch.21` -> `Vol.3`: the prefix some sources put before the chapter name."""
    found = _VOLUME_PREFIX.match(name) if isinstance(name, str) else None
    return f"Vol.{found.group(1)}" if found else None


class SuwayomiService:
    wake_target: str | None = None

    def __init__(
        self,
        base_url: str,
        *,
        refresh_mode: Literal["always_on", "driven"] = "always_on",
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._now = now  # the bound of an upload date (ENF-TIME-001)
        self.refresh_mode = refresh_mode
        self.manifest = build_manifest()
        self.connected = (
            ConnectedCapability.CATEGORIES
            | ConnectedCapability.SERVER_QUEUE
            | ConnectedCapability.UPSTREAM_RESTORE
            | ConnectedCapability.HEALTH_PROBE
            | ConnectedCapability.AUTH_MODE
            | ConnectedCapability.RELEASE_CLAIM
            | ConnectedCapability.INSTANCE_FINGERPRINT
        )
        self._mode_verified = False
        self._mode = ""  # the authentication mode last read from the server
        self._handles: dict[str, int] = {}  # ticket token -> last known local chapter id
        self._mangas: tuple[float, list[dict[str, Any]]] | None = None
        self._probed_at: float | None = None  # monotonic time of the last fingerprint read

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
        self._raise_for_status(response, f)
        try:
            payload = json.loads(response.body)
        except (ValueError, RecursionError) as exc:  # a body nested past the stack is no JSON
            raise SourceChanged("the server did not answer with JSON") from exc
        if not isinstance(payload, dict):
            raise SourceChanged("unexpected GraphQL envelope")
        return payload

    @staticmethod
    def _refused(f: Fetcher) -> AuthRequired:
        """Credentials were given and refused: the session is no longer good (`auth_expired`);
        without credentials, they are simply required."""
        if getattr(f, "has_credentials", False):
            return AuthExpired("the server refused the credentials")
        return AuthRequired("the server requires credentials")

    @classmethod
    def _raise_for_status(cls, response: Response, f: Fetcher) -> None:
        status = response.status
        if status == 401:
            if b"csrf" in response.body.lower():
                raise ServiceVersionMismatch("CSRF mismatch: not an authentication problem")
            raise cls._refused(f)
        if status == 429:
            raise RateLimited("rate limited by the server")
        if status >= 500:
            raise TemporarilyUnavailable(f"the server answered {status}")
        if status >= 400:
            raise SourceChanged(f"unexpected answer {status}")

    @staticmethod
    def _unauthorized(payload: dict[str, Any]) -> bool:
        errors = payload.get("errors")
        if not isinstance(errors, list):
            return False
        for error in errors:
            if not isinstance(error, dict):
                continue
            extensions = error.get("extensions")
            classification = (
                extensions.get("classification") if isinstance(extensions, dict) else ""
            )
            if str(error.get("message", "")).strip().casefold() == "unauthorized" or (
                str(classification).upper() == "UNAUTHORIZED"
            ):
                return True
        return False

    async def _query(
        self, f: Fetcher, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """A data operation: refused first if the tool runs in a mode this release cannot drive."""
        await self._ensure_supported_mode(f)
        payload = await self._post(f, query, variables)
        if self._unauthorized(payload):
            raise self._refused(f)
        if payload.get("errors") or not isinstance(payload.get("data"), dict):
            raise SourceChanged("GraphQL error: " + str(payload.get("errors"))[:200])
        data: dict[str, Any] = payload["data"]
        return data

    # -- EF-SRC-031: authentication mode -----------------------------------------------------
    async def auth_mode(self, f: Fetcher) -> str:
        """`none`, `basic_auth`, or the mode the server reports (`ui_login`...), which is refused.

        The reported `authMode` comes first. Without it, a 200 answer carrying an `Unauthorized`
        error means a token-only mode, hence `ui_login`: per S5 this fallback is the real path in
        `ui_login`, where an unauthenticated GraphQL call answers exactly that. It is not verified
        for this very query.
        """
        payload = await self._post(f, q.AUTH_MODE)
        data = payload.get("data")
        settings = (data.get("settings") if isinstance(data, dict) else None) or {}
        reported = settings.get("authMode")
        if isinstance(reported, str) and reported:
            self._mode = reported.lower()
            return self._mode
        if self._unauthorized(payload):
            self._mode = "ui_login"
            return self._mode
        raise SourceChanged("the server did not report its authentication mode")

    async def download_dir(self, f: Fetcher) -> str | None:
        """Where the server writes its downloads, as the server sees it (read by the switch-over
        detection, EF-EXP-044). None when it does not say; its default is an empty string."""
        data = await self._query(f, q.DOWNLOADS_PATH)
        settings = data.get("settings")
        path = settings.get("downloadsPath") if isinstance(settings, dict) else None
        return path if isinstance(path, str) else None  # "" is the default: not set

    async def _ensure_supported_mode(self, f: Fetcher) -> None:
        if self._mode_verified:
            return
        mode = await self.auth_mode(f)
        if mode not in SUPPORTED_MODES:
            raise AuthModeUnsupported()
        if mode == "ui_login":
            await self.ensure_least_privilege(f)
        self._mode_verified = True

    async def ensure_least_privilege(self, f: Fetcher) -> bool:
        """D1: the account Shelfhold logs in with must not be able to administer Suwayomi nor
        install extensions. True: the roles were read and are harmless. False: the server does not
        expose them at all (v2.4.2366, S5): nothing can be checked, the admin is told (the caller
        records `roles_unverified`). Anything else, a role that is too strong or an answer that
        cannot be understood, pauses the service: an unreadable answer is never an acceptance."""
        roles = await self.account_roles(f)
        if roles is None:
            return False
        for role in roles:
            if _installs_extensions(role):
                raise AuthModeUnsupported(
                    "le compte de service est administrateur ou peut installer des extensions "
                    f"Suwayomi (rôle {role[:40]!r}) : utilisez un compte aux droits minimaux"
                )
        return True

    async def account_roles(self, f: Fetcher) -> list[str] | None:
        """The role names of the logged-in account, or None when the server does not expose them.

        None only for a GraphQL *validation* error on the `user` / `roles` fields (HTTP 200, the
        field is unknown). A null `user`, any other error, or a shape that is not a list of names
        or of objects carrying one is `AuthModeUnsupported`: the check could not be done."""
        payload = await self._post(f, q.ACCOUNT_ROLES)
        if self._unauthorized(payload):
            raise self._refused(f)
        errors = payload.get("errors")
        if errors:
            if _only_unknown_field_errors(errors):
                return None  # the schema has no such field: the roles are not exposed
            raise AuthModeUnsupported(_ROLES_UNREADABLE)
        data = payload.get("data")
        user = data.get("user") if isinstance(data, dict) else None
        roles = user.get("roles") if isinstance(user, dict) else None
        if isinstance(roles, dict) and isinstance(roles.get("nodes"), list):
            roles = roles["nodes"]
        names = [_role_name(role) for role in roles] if isinstance(roles, list) else None
        if names is None or None in names:
            raise AuthModeUnsupported(_ROLES_UNREADABLE)
        return [n for n in names if n is not None]

    # -- HEALTH_PROBE ------------------------------------------------------------------------
    async def semantic_health(self, f: Fetcher) -> ServiceHealth:
        about = ((await self._post(f, q.ABOUT)).get("data") or {}).get("aboutServer") or {}
        version = str(about.get("version", ""))
        if not is_supported(version):
            return ServiceHealth(ok=False, version=version, detail="source.version_unsupported")
        if await self.auth_mode(f) not in SUPPORTED_MODES:
            return ServiceHealth(ok=False, version=version, detail="source.auth_mode_unsupported")
        return ServiceHealth(ok=True, version=version)

    async def unloaded_sources(self, f: Fetcher, source_keys: list[str]) -> list[str]:
        """EF-SRC-026 (a), read only: the upstream sources among `source_keys` that the server
        cannot use now. Absent (the extension is gone), a stub the server put in its place
        (`Source not installed: <id>`), or an incomplete record (an extension caught between two
        versions: no name or no language). Nothing is installed, nothing is written."""
        listed = (await self._query(f, q.SOURCES))["sources"]["nodes"]
        nodes = {str(n["id"]): n for n in listed}
        return [key for key in source_keys if _unusable(nodes.get(key.partition(":")[2]))]

    async def download_queue_size(self, f: Fetcher) -> int:
        """EF-SRC-026 (b): how many chapters the server's download queue holds."""
        status = (await self._query(f, q.DOWNLOAD_QUEUE))["downloadStatus"]
        return len(status.get("queue") or [])

    async def blocking_update_filters(self, f: Fetcher) -> list[str]:
        """EF-SRC-026 (d): the global update settings that exclude the series that are unread or
        not started, by name. They would keep Suwayomi from ever refreshing a followed series.
        Empty when the server does not expose them."""
        payload = await self._post(f, q.UPDATE_FILTERS)
        errors = payload.get("errors")
        if (
            isinstance(errors, list)
            and errors
            and all(
                isinstance(e, dict) and _UNKNOWN_FIELD.search(str(e.get("message", "")))
                for e in errors
            )
        ):
            return []  # a schema without these settings: nothing to warn about
        if errors:
            raise SourceChanged("GraphQL error: " + str(errors)[:200])
        settings = (payload.get("data") or {}).get("settings") or {}
        return [
            name for name in ("excludeNotStarted", "excludeUnreadChapters") if settings.get(name)
        ]

    async def request_refresh(self, f: Fetcher, refs: list[SeriesRef]) -> None:
        """Always-on: Suwayomi follows its own schedule and nothing is sent. Driven (piloted,
        EF-SRC-025 a): `fetchChapters` for each series, which returns once Suwayomi has asked the
        source. The pace between two series of one source is the core's (`limit_key`)."""
        if self.refresh_mode != "driven":
            return None
        for ref in refs:
            manga = await self._manga(f, ref)
            await self._query(f, q.FETCH_CHAPTERS, {"id": int(manga["id"])})

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
            cover=ImageRequest(url=self.base_url + q.THUMBNAIL_PATH.format(id=int(node["id"]))),
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

    def begin_cycle(self) -> None:
        """Forget what was read about the library: the next follow cycle starts fresh."""
        self._mangas = None

    async def _all_mangas(self, f: Fetcher, *, fresh: bool = False) -> list[dict[str, Any]]:
        """Every manga, read once per cycle: one query for the library, not one per series."""
        now = time.monotonic()
        if fresh or self._mangas is None or now - self._mangas[0] > MANGA_CACHE_SECONDS:
            data = await self._query(f, q.ALL_MANGAS)
            self._mangas = (now, list(data["mangas"]["nodes"]))
        return self._mangas[1]

    async def _manga(self, f: Fetcher, ref: SeriesRef) -> dict[str, Any]:
        """The manga of a reference, found by `(sourceId, manga.url)`, never by its local id.

        A miss in the cache is checked once against the server: the cache never hides a manga.
        """
        for fresh in (False, True):
            for node in await self._all_mangas(f, fresh=fresh):
                same = f"{KEY}:{node['sourceId']}" == ref.source_key
                if same and str(node["url"]) == ref.remote_id:
                    found: dict[str, Any] = node
                    return found
        raise ContentRemoved(f"{ref.remote_id} is no longer known to the server")

    async def get_series(self, f: Fetcher, ref: SeriesRef) -> SeriesInfo:
        node = await self._manga(f, ref)
        author, artist = node.get("author"), node.get("artist")
        return SeriesInfo(
            ref=self._ref(node),
            title=str(node["title"]),
            description=node.get("description") or None,
            authors=[str(author)] if author else [],
            artists=[str(artist)] if artist else [],
        )

    async def followed_series(self, f: Fetcher) -> list[SeriesRef]:
        return list((await self.search(f, "", {}, None)).items)

    async def set_follow(
        self, f: Fetcher, ref: SeriesRef, on: bool, *, remove_upstream: bool = False
    ) -> None:
        """Follow or unfollow, both idempotent (EF-SRC-029).

        A series Suwayomi already holds in its library is linked as it is: nothing is written, the
        user's own categories are not touched. One that is known but not in the library enters it,
        in the `Shelfhold` category, which Suwayomi's own schedule leaves out. Unfollowing lets go
        of the category and keeps the series in Suwayomi, unless `remove_upstream` is asked for."""
        node = await self._manga(f, ref)
        manga_id = int(node["id"])
        state = (await self._query(f, q.MANGA_LIBRARY_STATE, {"id": manga_id})).get("manga") or {}
        in_library = bool(state.get("inLibrary"))
        held = {int(c["id"]) for c in (state.get("categories") or {}).get("nodes") or []}
        try:
            if on:
                if in_library:
                    return  # attached to what is there: never added twice
                category = await self._shelfhold_category(f, create=True)
                await self._query(f, q.SET_LIBRARY, {"id": manga_id, "inLibrary": True})
                if category is not None and category not in held:
                    await self._query(
                        f, q.SET_MANGA_CATEGORIES, {"id": manga_id, "add": [category], "remove": []}
                    )
            elif remove_upstream:
                if in_library:
                    await self._query(f, q.SET_LIBRARY, {"id": manga_id, "inLibrary": False})
            else:
                category = await self._shelfhold_category(f, create=False)
                if category is not None and category in held:
                    await self._query(
                        f, q.SET_MANGA_CATEGORIES, {"id": manga_id, "add": [], "remove": [category]}
                    )
        finally:
            self._mangas = None  # the library moved: the next read is fresh

    async def _shelfhold_category(self, f: Fetcher, *, create: bool) -> int | None:
        """The id of the `Shelfhold` category, created once if `create` and missing. It must be
        excluded from the scheduled updates: one that is not is refused, never changed."""
        for attempt in (0, 1):
            found = await self._find_category(f)
            if found is not None:
                category = int(found["id"])
                if create:
                    await self._exclude_from_download(f, category)
                return category
            if not create or attempt:
                break
            if not self._instance_probed():
                # a Suwayomi installed again has no category, and making one before the
                # fingerprint was looked at would hide it (EF-SRC-036)
                raise TemporarilyUnavailable("the instance was not probed in this cycle")
            await self._query(f, q.CREATE_SHELFHOLD_CATEGORY)
        if create:
            raise SourceChanged("the Shelfhold category could not be created")
        return None

    async def _exclude_from_download(self, f: Fetcher, category: int) -> None:
        """The category holds its series out of the automatic download, and `includeInUpdate` is
        never written. Set once, read back: a server that did not take it is a changed schema."""
        nodes = (await self._query(f, q.CATEGORY_DOWNLOAD))["categories"]["nodes"]
        mine = next((c for c in nodes if int(c["id"]) == category), None)
        if mine is not None and str(mine.get("includeInDownload", "")).upper() == "EXCLUDE":
            return
        await self._query(f, q.EXCLUDE_CATEGORY_FROM_DOWNLOAD, {"id": category})
        nodes = (await self._query(f, q.CATEGORY_DOWNLOAD))["categories"]["nodes"]
        mine = next((c for c in nodes if int(c["id"]) == category), None)
        if mine is None or str(mine.get("includeInDownload", "")).upper() != "EXCLUDE":
            raise SourceChanged("the Shelfhold category could not be excluded from the download")

    def _instance_probed(self) -> bool:
        """The fingerprint was read lately: a follow runs after the cycle's probe, never before."""
        return (
            self._probed_at is not None
            and time.monotonic() - self._probed_at <= PROBE_VALID_SECONDS
        )

    async def _find_category(self, f: Fetcher) -> dict[str, Any] | None:
        """The category named `Shelfhold` (the lowest id if two): a read, nothing is written."""
        nodes = (await self._query(f, q.CATEGORIES))["categories"]["nodes"]
        named = sorted((c for c in nodes if str(c.get("name")) == MARK), key=lambda c: c["id"])
        return dict(named[0]) if named else None

    # -- INSTANCE_FINGERPRINT (EF-SRC-036) ---------------------------------------------------
    async def instance_fingerprint(self, f: Fetcher) -> str:
        """What tells this installation from another: the identity of the category Shelfhold made
        in it. Following moves nothing; a new database does not have it. `""` while there is none
        (nothing has been followed yet)."""
        found = await self._find_category(f)
        self._probed_at = time.monotonic()
        if found is None:
            return ""
        return hashlib.sha256(f"{KEY}|{MARK}|{int(found['id'])}".encode()).hexdigest()[:32]

    # -- UPSTREAM_RESTORE (EF-EXP-056) -------------------------------------------------------
    # The closed shape of a snapshot (ids, names and keywords): nothing else is ever stored.
    snapshot_schema: ClassVar[dict[str, Any]] = {
        "categories": [{"id": int, "name": str, "include_in_update": str}],
        "update_strategy": str,
        "shelfhold_excluded?": [int],
    }

    async def snapshot_upstream(self, f: Fetcher, ref: SeriesRef) -> dict[str, object]:
        """What decides whether Suwayomi's own schedule updates the series: its categories and
        its update strategy. Ids, names and keywords; nothing secret."""
        node = await self._manga(f, ref)
        data = await self._query(f, q.MANGA_SETTINGS, {"id": node["id"]})
        manga = data.get("manga")
        if not isinstance(manga, dict):
            raise ContentRemoved(f"{ref.remote_id} is no longer known to the server")
        categories = (manga.get("categories") or {}).get("nodes") or []
        return {
            "categories": [
                {
                    "id": int(c["id"]),
                    "name": str(c.get("name", "")),
                    "include_in_update": str(c.get("includeInUpdate", "UNSET")).upper(),
                }
                for c in categories
            ],
            "update_strategy": str(manga.get("updateStrategy") or "ALWAYS_UPDATE"),
        }

    @staticmethod
    def snapshot_valid(snap: dict[str, object]) -> bool:
        """A snapshot that is not what `snapshot_upstream` writes is not trusted: the
        strategy must be a known keyword and every category an integer id."""
        strategy = snap.get("update_strategy", "ALWAYS_UPDATE")
        categories = snap.get("categories", [])
        excluded = snap.get("shelfhold_excluded", [])
        return (
            strategy in UPDATE_STRATEGIES
            and isinstance(categories, list)
            and all(isinstance(c, dict) and type(c.get("id")) is int for c in categories)
            and isinstance(excluded, list)
            and all(type(i) is int for i in excluded)
        )

    async def restore_upstream(self, f: Fetcher, ref: SeriesRef, snap: dict[str, object]) -> None:
        """Give the series back to Suwayomi's schedule. The category that excluded it from
        updates goes: only the one Shelfhold recorded (`shelfhold_excluded`); with no record,
        only an exclusion that bears its mark (the category named `Shelfhold`). The categories
        and the update strategy of the snapshot come back. Nothing is written for an invalid
        snapshot (it counts as none), and the restoration is only done if the series is no
        longer held out by the removed ones."""
        if not self.snapshot_valid(snap):
            snap = {}
        node = await self._manga(f, ref)
        manga_id = int(node["id"])
        current = (await self._query(f, q.MANGA_SETTINGS, {"id": manga_id})).get("manga") or {}
        have = {int(c["id"]) for c in (current.get("categories") or {}).get("nodes") or []}
        nodes = (await self._query(f, q.CATEGORIES))["categories"]["nodes"]
        known = {int(c["id"]): str(c.get("includeInUpdate", "")).upper() for c in nodes}
        named = {int(c["id"]): str(c.get("name", "")) for c in nodes}
        wanted_raw = snap.get("categories")
        wanted = {
            int(c["id"]) for c in (wanted_raw if isinstance(wanted_raw, list) else [])
            if int(c["id"]) in known
        }  # fmt: skip
        recorded = snap.get("shelfhold_excluded")
        if isinstance(recorded, list):
            remove = sorted(i for i in have - wanted if i in recorded)
        else:
            # No record (an installation from before the records): only the exclusion that bears
            # Shelfhold's mark goes; any other may be the administrator's own and stays.
            remove = sorted(
                i for i in have - wanted if known.get(i) == "EXCLUDE" and named.get(i) == MARK
            )
        add = sorted(wanted - have)
        if add or remove:
            await self._query(
                f, q.SET_MANGA_CATEGORIES, {"id": manga_id, "add": add, "remove": remove}
            )
        strategy = snap.get("update_strategy")
        if isinstance(strategy, str) and strategy != str(current.get("updateStrategy")):
            await self._query(f, q.SET_UPDATE_STRATEGY, {"id": manga_id, "strategy": strategy})
        after = (await self._query(f, q.MANGA_SETTINGS, {"id": manga_id})).get("manga") or {}
        left = {int(c["id"]) for c in (after.get("categories") or {}).get("nodes") or []}
        if left & set(remove):
            raise SourceChanged("the series is still held out of the updates")

    async def kept_exclusions(self, f: Fetcher, ref: SeriesRef) -> list[str]:
        """The names of the categories that still hold the series out of the scheduled updates
        after a restoration: the administrator's own, which a detachment leaves and says."""
        node = await self._manga(f, ref)
        current = (await self._query(f, q.MANGA_SETTINGS, {"id": int(node["id"])})).get("manga")
        categories = (current or {}).get("categories") or {}
        return [
            str(c.get("name", c["id"]))
            for c in categories.get("nodes") or []
            if str(c.get("includeInUpdate", "")).upper() == "EXCLUDE"
        ]

    # -- units -------------------------------------------------------------------------------
    @staticmethod
    def _unit_id(source_key: str, chapter_url: str) -> str:
        return f"{source_key}:{chapter_url}"

    async def _chapters(self, f: Fetcher, ref: SeriesRef) -> list[dict[str, Any]]:
        for attempt in (0, 1):
            manga = await self._manga(f, ref)
            data = await self._query(f, q.CHAPTERS, {"mangaId": manga["id"]})
            nodes: list[dict[str, Any]] = data["chapters"]["nodes"]
            if nodes or attempt:
                return nodes
            self.begin_cycle()  # empty: maybe a stale cached id (reinstall), look once more
        return []

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
            volume_label=_volume_label(chapter.get("name")),
            team=chapter.get("scanlator") or None,
            published_at=_upload_date(chapter.get("uploadDate"), self._now()),
            memo={"fetch_handle": chapter["id"], "chapter_url": chapter["url"]},
        )

    async def _current_handle(
        self, f: Fetcher, ref: SeriesRef, chapter_url: str, hint: int | None
    ) -> int:
        """The local chapter id now: the cached handle if it still names this chapter, else
        re-resolved by `(sourceId, chapter.url)` (after a reinstall or a restore, EF-SRC-036)."""
        if hint is not None:
            node = (await self._query(f, q.CHAPTER, {"id": hint}))["chapter"]
            if node and await self._matches(f, ref, node, chapter_url):
                return hint
        for chapter in await self._chapters(f, ref):
            if str(chapter["url"]) == chapter_url:
                return int(chapter["id"])
        raise ContentRemoved(f"{chapter_url} is no longer known to the server")

    async def _matches(
        self, f: Fetcher, ref: SeriesRef, node: dict[str, Any], chapter_url: str
    ) -> bool:
        """A cached handle still names this chapter: same url *and* same manga (two series can
        share a chapter url after a reinstall)."""
        manga = await self._manga(f, ref)
        return node.get("url") == chapter_url and node.get("mangaId") == manga["id"]

    def _asset(
        self, handle: int, unit_id: str, number: str | None, name: str | None, chapter_url: str
    ) -> FileAsset:
        return FileAsset(
            request=ImageRequest(url=self.base_url + q.DOWNLOAD_PATH.format(id=handle)),
            mime=CBZ_MIME,
            filename=_filename(unit_id, number, name or chapter_url.rsplit("/", 1)[-1]),
        )

    async def fetch_unit(self, f: Fetcher, ref: SeriesRef, u: UnitInfo) -> UnitPayload:
        chapter_url = _chapter_url(u)
        hint = u.memo.get("fetch_handle")
        handle = await self._current_handle(
            f, ref, chapter_url, hint if isinstance(hint, int) else None
        )
        return self._asset(handle, u.remote_unit_id, u.number, u.title, chapter_url)

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
            if node and await self._matches(f, ref, node, chapter_url):
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
        ref, chapter_url = self._decode(t)
        number = found[1].get("chapterNumber")
        label = format(Decimal(str(number)).normalize(), "f") if _known_number(number) else None
        unit_id = self._unit_id(ref.source_key, chapter_url)
        return self._asset(found[0], unit_id, label, found[1].get("name"), chapter_url)

    async def cancel(self, f: Fetcher, t: Ticket) -> None:
        found = await self._ticket_node(f, t)
        if found is not None:
            await self._query(f, q.DEQUEUE, {"id": found[0]})

    # -- RELEASE_CLAIM -----------------------------------------------------------------------
    async def release(self, f: Fetcher, t: Ticket) -> ReleaseResult:
        """Erase the downloaded chapter of this ticket on the Suwayomi side (EF-BIB-038): an
        erasure upstream, irreversible. The only caller is the purge of L16, after its guards (a
        kept copy that still counts). A chapter that is not downloaded, or gone, erases nothing.

        In `ui_login` the downloads are shared between accounts, each holding a reference: the
        erasure only withdraws the claim of the service account and the file stays while another
        account keeps it. What it freed is then measured, size before and presence after, never
        supposed (EF-SRC-031); the library of another account is not looked at."""
        found = await self._ticket_node(f, t)
        if found is None or not found[1].get("isDownloaded"):
            return ReleaseResult(released=False)
        shared = self._mode == "ui_login"
        size = await self._archive_size(f, found[0]) if shared else None
        await self._query(f, q.DELETE_DOWNLOADED_CHAPTER, {"id": found[0]})
        if not shared:
            return ReleaseResult(released=True)
        after = await self._ticket_node(f, t)
        present = after is not None and bool(after[1].get("isDownloaded"))
        return ReleaseResult(
            released=True, freed_bytes=0 if present else (size or 0), still_present=present
        )

    async def _archive_size(self, f: Fetcher, chapter_id: int) -> int:
        """The size of the file as the server serves it, before it is erased."""
        url = self.base_url + q.DOWNLOAD_PATH.format(id=chapter_id)
        return (await f.download(ImageRequest(url=url))).size


def _unusable(node: dict[str, Any] | None) -> bool:
    """A source record the server cannot serve from: missing, a stub, or half there."""
    if node is None:
        return True
    name, lang = node.get("displayName"), node.get("lang")
    if not isinstance(name, str) or not name.strip() or not isinstance(lang, str) or not lang:
        return True
    return name.lower().startswith("source not installed")


def _installs_extensions(role: str) -> bool:
    """An administrator or equivalent, or any right over extensions (case-insensitive)."""
    name = role.strip().upper()
    return any(word in name for word in _PRIVILEGED_WORDS)


def _role_name(role: object) -> str | None:
    """A role given as a string, or as an object carrying its name; None when it is neither."""
    if isinstance(role, str):
        return role
    if isinstance(role, dict):
        for key in _ROLE_KEYS:
            if isinstance(role.get(key), str):
                return str(role[key])
    return None


def _only_unknown_field_errors(errors: object) -> bool:
    """Every error is a validation error about the `user` or `roles` field being unknown."""
    if not isinstance(errors, list) or not errors:
        return False
    for error in errors:
        message = str(error.get("message", "")) if isinstance(error, dict) else ""
        mentions = re.search(r"['\"\s@\[]\b(user|roles)\b", message, re.IGNORECASE)
        if not (_UNKNOWN_FIELD.search(message) and mentions):
            return False
    return True


def _chapter_url(unit: UnitInfo) -> str:
    memo_url = unit.memo.get("chapter_url")
    if isinstance(memo_url, str):
        return memo_url
    _, _, rest = unit.remote_unit_id.partition(":")
    return rest.split(":", 1)[1] if ":" in rest else rest


def _known_number(number: Any) -> bool:
    return isinstance(number, int | float) and number >= 0


def _clean_name(raw: str) -> str:
    """A file name kept as Unicode when safe: no separators, controls or reserved characters."""
    text = unicodedata.normalize("NFC", raw)
    text = "".join(" " if unicodedata.category(c).startswith(("C", "Z")) else c for c in text)
    text = _FORBIDDEN_NAME.sub("_", text)
    text = " ".join(text.split()).strip(" .")
    return text.removesuffix(".cbz").strip(" .")


def _filename(unit_id: str, number: str | None, name: str) -> str:
    """`<number or short hash> - <sanitised name>.cbz`, at most ~120 bytes."""
    prefix = number if number else hashlib.sha256(unit_id.encode()).hexdigest()[:8]
    prefix = _clean_name(prefix) or hashlib.sha256(unit_id.encode()).hexdigest()[:8]
    budget = MAX_FILENAME_BYTES - len(b".cbz")
    stem = f"{prefix} - {_clean_name(name)}" if _clean_name(name) else prefix
    while len(stem.encode()) > budget:
        stem = stem[:-1]
    return f"{stem.rstrip(' .-')}.cbz"


def _lang(raw: str) -> LanguageTag | Literal["mul"]:
    if raw.casefold() in ("all", "multi", ""):
        return "mul"
    try:
        return normalize_lang(raw).tag
    except LanguageError:
        return "mul"


_ZERO_TEXTS = frozenset({"", "0", "0000", "0000-00", "0000-00-00"})
_MS_THRESHOLD = 100_000_000_000  # above this a Unix timestamp is in milliseconds (year 5138 in s)


def _upload_date(raw: Any, now: datetime) -> datetime | None:
    """The upload date (Unix seconds or milliseconds), or None when it is null, zero, negative,
    a boolean, NaN, infinite, out of range, unreadable, or later than `now` plus the clock skew
    (ENF-TIME-001): a source cannot claim a release in 2099."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if raw in _ZERO_TEXTS:
            return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number >= _MS_THRESHOLD:
        number /= 1000
    try:
        value = datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return None if value > now + DEFAULT_SKEW else value
