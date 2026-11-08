"""A simulated Suwayomi server, in process, synthetic only (EF-SRC-008, ENF-LEG-013).

It answers what the spike S5 measured against the pinned v2.4.2366 (`spikes/s5/RESULTATS.md`):
GraphQL in JSON on `/api/graphql`, the chapter archive on `GET /api/v1/chapter/{id}/download`,
`basic_auth` (HTTP 401 without credentials) and `ui_login` (GraphQL answers 200 with an
`Unauthorized` error, REST answers 401, `aboutServer` stays public).

It also plays the host's referee: any request outside the allow-list (route, JSON-only POST,
GraphQL root field) is answered 400 and recorded in `violations`. The allow-list is written here
on purpose, independently of the adapter under test.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2

from shelfhold.sdk.fetcher import CookieJarView, DownloadedFile, Response
from shelfhold.sdk.refs import ImageRequest

TESTS = Path(__file__).resolve().parents[2]
STATE = TESTS / "fixtures" / "suwayomi_states" / "library.json"
SYNTHETIC = TESTS / "fixtures" / "synthetic"
BASE_URL = "http://suwayomi.lan:4567"
USER = "shelfhold"
PASSWORD = "synthetic-test-password"  # noqa: S105 - throwaway value of the simulated server
RENUMBER_OFFSET = 1000  # a reinstall gives every chapter and manga a new local integer

ALLOWED_ROUTES = (
    re.compile(r"^/api/graphql$"),
    re.compile(r"^/api/v1/chapter/\d+/download$"),
    re.compile(r"^/api/v1/manga/\d+/thumbnail$"),
)
# GraphQL root fields the adapter may use in M1a, and the ones it must never use.
ALLOWED_FIELDS = frozenset({
    "aboutServer", "settings", "sources", "mangas", "manga", "chapters", "chapter",
    "enqueueChapterDownload", "dequeueChapterDownload", "updateManga",
    "categories", "updateMangaCategories", "createCategory", "updateCategory",
    "deleteDownloadedChapter",
    "login", "refreshToken",  # M1b L20: the JWT pair of `ui_login`, in the exact S5 forms
    "user",  # M1b L20: the roles of the logged-in account (D1), when the server exposes them
})  # fmt: skip
FORBIDDEN_FIELDS = frozenset({
    "fetchChapterPages", "fetchChapters", "fetchManga", "fetchSourceManga", "updateLibrary",
    "updateCategoryManga", "deleteDownloadedChapters", "deleteCategory",
    "clearDownloader", "startDownloader", "restoreBackup",
    "installExternalExtension", "updateExtension", "addExtensionStore", "setSettings",
})  # fmt: skip
_TOKEN = re.compile(r"[A-Za-z_]\w*")
# The S5 forms (`spikes/s5`, `harness.login` and the contract test), nothing looser:
_LOGIN = re.compile(
    r'^\s*mutation\s*\{\s*login\(input:\s*\{username:\s*(?P<u>"(?:[^"\\]|\\.)*"),\s*'
    r'password:\s*(?P<p>"(?:[^"\\]|\\.)*")\}\)\s*\{\s*accessToken\s+refreshToken\s*\}\s*\}\s*$'
)
_REFRESH = re.compile(
    r'^\s*mutation\s*\{\s*refreshToken\(input:\s*\{refreshToken:\s*(?P<t>"(?:[^"\\]|\\.)*")\}\)'
    r"\s*\{\s*accessToken\s*\}\s*\}\s*$"
)


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes = b""
    fields: frozenset[str] = frozenset()  # GraphQL root fields found in the document

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class FakeAccount:
    """One account of a `ui_login` server: its own password, roles and library (series urls)."""

    password: str
    roles: list[str] = field(default_factory=lambda: ["USER"])
    library: set[str] | None = None  # None: the series flagged `inLibrary` in the state


@dataclass
class FakeSuwayomi:
    """The server and its state; `handle` is the whole HTTP surface."""

    auth_mode: str = "basic_auth"  # "basic_auth" | "ui_login" | "none"
    version: str = "v2.4.2366"
    csrf_always: bool = False  # every POST is answered 401 "CSRF mismatch"
    state: dict[str, Any] = field(default_factory=lambda: json.loads(STATE.read_text("utf-8")))
    requests: list[RecordedRequest] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    _polls: dict[int, int] = field(default_factory=dict)
    _downloaded: set[int] = field(default_factory=set)
    _enqueued: set[int] = field(default_factory=set)
    categories: list[dict[str, Any]] = field(default_factory=list)  # id, name, includeInUpdate
    updated_by_schedule: list[int] = field(default_factory=list)  # manga ids, in order
    deleted: list[int] = field(default_factory=list)  # chapter ids of `deleteDownloadedChapter`
    # -- ui_login (M1b L20): accounts, shared downloads with references, the JWT pair -------
    accounts: dict[str, FakeAccount] = field(default_factory=dict)
    roles_payload: dict[str, Any] | None = None  # a raw answer to the roles query (hostile cases)
    expose_roles: bool = False  # v2.4.2366 exposes none (S5): the roles query is a GraphQL error
    claims: dict[int, set[str]] = field(default_factory=dict)  # chapter id -> accounts holding it
    files_removed: list[int] = field(default_factory=list)  # chapters whose file really went
    logins: int = 0
    login_users: list[str] = field(default_factory=list)
    refreshes: int = 0
    refresh_delay: Callable[[], Awaitable[None]] | None = None  # to overlap two refreshes
    rotate_refresh_token: bool = False  # a server that also answers a new refresh token
    reads_by_account: list[tuple[str, str]] = field(default_factory=list)  # (account, root field)
    _access: dict[str, str] = field(default_factory=dict)  # access token -> account
    _refresh: dict[str, str] = field(default_factory=dict)  # refresh token -> account
    _issued: int = 0
    _pairs: list[tuple[str, str]] = field(default_factory=list)
    _account: str = ""  # the account of the request being answered

    def __post_init__(self) -> None:
        if self.auth_mode == "ui_login" and USER not in self.accounts:
            self.accounts[USER] = FakeAccount(PASSWORD)

    # -- scenario helpers -------------------------------------------------------------------
    def add_account(
        self,
        name: str,
        password: str,
        *,
        roles: list[str] | None = None,
        library: set[str] | None = None,
    ) -> None:
        """Another account of the server: it has a library the adapter must never read."""
        self.accounts[name] = FakeAccount(password, roles or ["USER"], library or set())

    def hold(self, account: str, chapter_url: str) -> int:
        """`account` keeps this chapter downloaded (a reference on the shared file)."""
        chapter_id = self.local_chapter_ids()[chapter_url]
        self.claims.setdefault(chapter_id, set()).add(account)
        self._downloaded.add(chapter_id)
        return chapter_id

    def is_downloaded(self, chapter_url: str) -> bool:
        return self.local_chapter_ids()[chapter_url] in self._downloaded

    def issued_tokens(self) -> list[tuple[str, str]]:
        """Every (access, refresh) pair ever issued, oldest first."""
        return list(self._pairs)

    def expire_access_tokens(self) -> None:
        """The 5-minute JWT ran out: every access token is refused, refresh tokens still live."""
        self._access.clear()

    def expire_all_tokens(self) -> None:
        self._access.clear()
        self._refresh.clear()

    def account_library(self, account: str) -> set[str]:
        held = self.accounts[account].library
        if held is None:
            held = {m["url"] for m in self.state["mangas"] if m["inLibrary"]}
            self.accounts[account].library = held
        return held

    # -- (scenario helpers continue) --------------------------------------------------------
    def reinstall(self, *, wipe: bool = False) -> None:
        """Same tool, same upstream sources, but every local integer id is new (EF-SRC-036).

        `wipe`: the database is recreated empty, as a fresh installation is (no category, no
        series in the library); the sources and their chapters stay known."""
        if wipe:
            self.categories.clear()
            for manga in self.state["mangas"]:
                manga["inLibrary"] = False
                manga["categoryIds"] = []
        self._polls.clear()
        self._downloaded.clear()
        self._enqueued.clear()
        for manga in self.state["mangas"]:
            manga["id"] += RENUMBER_OFFSET
            for chapter in manga["chapters"]:
                chapter["id"] += RENUMBER_OFFSET

    def add_category(
        self, name: str, *, include_in_update: str = "UNSET", include_in_download: str = "UNSET"
    ) -> int:
        """A category of the library (`EXCLUDE`: its series are left out of scheduled updates)."""
        ident = 1 + max((c["id"] for c in self.categories), default=0)
        self.categories.append(
            {
                "id": ident,
                "name": name,
                "includeInUpdate": include_in_update,
                "includeInDownload": include_in_download,
            }
        )
        return ident

    def assign(self, manga_url: str, category_id: int) -> None:
        manga = next(m for m in self.state["mangas"] if m["url"] == manga_url)
        manga.setdefault("categoryIds", []).append(category_id)

    def categories_of(self, manga_url: str) -> list[int]:
        manga = next(m for m in self.state["mangas"] if m["url"] == manga_url)
        return sorted(manga.get("categoryIds", []))

    def strategy_of(self, manga_url: str) -> str:
        manga = next(m for m in self.state["mangas"] if m["url"] == manga_url)
        return str(manga.get("updateStrategy", "ALWAYS_UPDATE"))

    def run_scheduled_update(self) -> list[int]:
        """Suwayomi's own schedule: every library series that is not excluded and whose update
        strategy is the default is looked at, and the manga ids that were are returned."""
        excluded = {c["id"] for c in self.categories if c["includeInUpdate"] == "EXCLUDE"}
        seen = [
            m["id"]
            for m in self.state["mangas"]
            if m["inLibrary"]
            and m.get("updateStrategy", "ALWAYS_UPDATE") == "ALWAYS_UPDATE"
            and not excluded & set(m.get("categoryIds", []))
        ]
        self.updated_by_schedule.extend(seen)
        return seen

    def add_source(self, source_id: str, name: str, lang: str = "en") -> None:
        self.state["sources"].append(
            {"id": source_id, "displayName": name, "lang": lang, "isNsfw": False}
        )

    def local_chapter_ids(self) -> dict[str, int]:
        """chapter.url -> current local id."""
        return {c["url"]: c["id"] for m in self.state["mangas"] for c in m["chapters"]}

    def cbz_bytes(self, chapter_url: str) -> bytes:
        for manga in self.state["mangas"]:
            for chapter in manga["chapters"]:
                if chapter["url"] == chapter_url:
                    return (SYNTHETIC / chapter["cbz"]).read_bytes()
        raise KeyError(chapter_url)

    def cbz_sha256(self, chapter_url: str) -> str:
        return hashlib.sha256(self.cbz_bytes(chapter_url)).hexdigest()

    def cbz_sha256_of_id(self, chapter_id: int) -> str:
        chapter = self._chapter(chapter_id)
        assert chapter is not None
        return hashlib.sha256((SYNTHETIC / chapter["cbz"]).read_bytes()).hexdigest()

    def requests_to(self, fragment: str) -> list[RecordedRequest]:
        return [r for r in self.requests if fragment in r.path]

    # -- HTTP -------------------------------------------------------------------------------
    def handle(
        self, method: str, target: str, headers: dict[str, str] | None = None, body: bytes = b""
    ) -> Response:
        lowered = {k.lower(): v for k, v in (headers or {}).items()}
        split = urlsplit(target)
        recorded = RecordedRequest(method, split.path, lowered, body)
        self.requests.append(recorded)
        if not any(route.match(split.path) for route in ALLOWED_ROUTES):
            return self._violation(f"{method} {split.path} is not an allow-listed route")
        if method == "POST":
            if not lowered.get("content-type", "").startswith("application/json"):
                return self._text(401, "CSRF mismatch")  # what a form POST gets in basic_auth
            if self.csrf_always:
                return self._text(401, "CSRF mismatch")
        denied = self._denied(lowered, split.path)
        if denied is not None:
            return denied
        if split.path == "/api/graphql":
            return self._graphql(recorded, authenticated=self._authorized(lowered))
        return self._download(split.path)

    def _violation(self, why: str) -> Response:
        self.violations.append(why)
        return self._text(400, why)

    @staticmethod
    def _text(status: int, text: str) -> Response:
        return Response(status=status, headers={"content-type": "text/plain"}, body=text.encode())

    @staticmethod
    def _json(payload: Any, status: int = 200) -> Response:
        return Response(
            status=status,
            headers={"content-type": "application/json"},
            body=json.dumps(payload).encode(),
        )

    def _authorized(self, headers: dict[str, str]) -> bool:
        if self.auth_mode == "none":
            return True
        if self.auth_mode == "basic_auth":
            pair = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
            return headers.get("authorization") == f"Basic {pair}"
        auth = headers.get("authorization", "")  # ui_login: JWT only
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None
        account = self._access.get(token) if token else None
        self._account = account or ""
        return account is not None

    def _denied(self, headers: dict[str, str], path: str) -> Response | None:
        if self._authorized(headers):
            return None
        if path == "/api/graphql" and self.auth_mode == "ui_login":
            return None  # GraphQL answers 200 with an error (aboutServer stays public)
        return self._text(401, "Unauthorized")

    # -- REST -------------------------------------------------------------------------------
    def _chapter(self, chapter_id: int) -> dict[str, Any] | None:
        for manga in self.state["mangas"]:
            for chapter in manga["chapters"]:
                if chapter["id"] == chapter_id:
                    return chapter
        return None

    def _download(self, path: str) -> Response:
        if path.endswith("/thumbnail"):
            return Response(status=200, headers={"content-type": "image/png"}, body=b"\x89PNG")
        chapter = self._chapter(int(path.split("/")[4]))
        if chapter is None:
            return self._text(400, "Invalid folder to create CBZ")  # as measured in S5
        # An HTTP header is latin-1: what is not (a bidi mark in a hostile title) is replaced.
        shown = str(chapter["name"]).encode("latin-1", "replace").decode("latin-1")
        return Response(
            status=200,
            headers={
                "content-type": "application/vnd.comicbook+zip",
                "content-disposition": f'attachment; filename="Series - {shown}.cbz"',
            },
            body=(SYNTHETIC / chapter["cbz"]).read_bytes(),
        )

    # -- GraphQL ----------------------------------------------------------------------------
    def _graphql(self, recorded: RecordedRequest, *, authenticated: bool) -> Response:
        try:
            payload = recorded.json()
            query: str = payload["query"]
        except (ValueError, KeyError, TypeError):
            return self._violation("GraphQL body is not a JSON object with a query")
        variables = payload.get("variables") or {}
        found = frozenset(_TOKEN.findall(query)) & (ALLOWED_FIELDS | FORBIDDEN_FIELDS)
        recorded.fields = found
        bad = found & FORBIDDEN_FIELDS
        if bad or not found:
            return self._violation(f"GraphQL fields not allow-listed: {sorted(bad) or 'none'}")
        if found & {"login", "refreshToken"}:
            return self._token_mutation(query)
        if not authenticated and found != {"aboutServer"}:
            return self._json({"data": None, "errors": [{"message": "Unauthorized"}]})
        if "user" in found and self.roles_payload is not None:
            return self._json(self.roles_payload)
        if "user" in found and not self.expose_roles:  # a schema without that field (S5)
            message = "Validation error: Field 'user' in type 'Query' is undefined"
            return self._json({"errors": [{"message": message}]})
        if self.auth_mode == "ui_login":
            self.reads_by_account.extend((self._account, n) for n in sorted(found))
        data: dict[str, Any] = {}
        for name in sorted(found):
            data[name] = getattr(self, f"_q_{name}")(query, variables)
        return self._json({"data": data})

    def _issue(self, account: str) -> tuple[str, str]:
        self._issued += 1
        access, refresh = f"acc.{self._issued}.{account}", f"ref.{self._issued}.{account}"
        self._access[access] = account
        self._refresh[refresh] = account
        self._pairs.append((access, refresh))
        return access, refresh

    def _token_mutation(self, query: str) -> Response:
        """`login` and `refreshToken`, accepted only in the S5 forms and only in `ui_login`."""
        login, refresh = _LOGIN.match(query), _REFRESH.match(query)
        if self.auth_mode != "ui_login" or not (login or refresh):
            return self._violation("login/refreshToken outside ui_login or not in the S5 forms")
        if login:
            self.logins += 1
            user, password = json.loads(login["u"]), json.loads(login["p"])
            self.login_users.append(user)
            account = self.accounts.get(user)
            if account is None or account.password != password:
                return self._json({"data": None, "errors": [{"message": "Invalid credentials"}]})
            access, refresh_token = self._issue(user)
            return self._json(
                {"data": {"login": {"accessToken": access, "refreshToken": refresh_token}}}
            )
        if refresh:
            self.refreshes += 1
            holder = self._refresh.get(json.loads(refresh["t"]))
            if holder is None:
                return self._json({"data": None, "errors": [{"message": "Invalid token"}]})
            access, new_refresh = self._issue(holder)
            payload = {"accessToken": access}
            if self.rotate_refresh_token:
                del self._refresh[json.loads(refresh["t"])]
                payload["refreshToken"] = new_refresh
            else:
                self._refresh.pop(new_refresh)
            return self._json({"data": {"refreshToken": payload}})
        raise AssertionError("unreachable")

    @staticmethod
    def _wanted_id(query: str, variables: dict[str, Any], key: str) -> int | None:
        for value in variables.values():
            if isinstance(value, dict) and key in value:
                return int(value[key])
            if isinstance(value, int | str) and str(value).isdigit():
                return int(value)
        match = re.search(rf"{key}\s*:\s*\"?(\d+)", query)
        match = match or re.search(r"\bid\s*:\s*\"?(\d+)", query)
        return int(match.group(1)) if match else None

    def _q_aboutServer(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {"name": self.state["name"], "version": self.version, "buildType": "Stable"}

    def _q_settings(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "authMode": self.auth_mode.upper(),
            "downloadsPath": self.state.get("downloads_path", "/downloads"),
        }

    def _q_user(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {"roles": list(self.accounts[self._account].roles)}

    def _q_sources(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {"nodes": copy.deepcopy(self.state["sources"])}

    def _in_library(self, manga: dict[str, Any]) -> bool:
        """The flag of the account that asks: libraries are per account in `ui_login`."""
        if self.auth_mode == "ui_login" and self._account:
            return bool(manga["url"] in self.account_library(self._account))
        return bool(manga["inLibrary"])

    @staticmethod
    def _manga_node(manga: dict[str, Any], *, library: bool | None = None) -> dict[str, Any]:
        node = {
            k: v for k, v in manga.items() if k not in ("chapters", "categoryIds", "updateStrategy")
        }
        if library is not None:
            node["inLibrary"] = library
        return node

    def _q_mangas(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        only_library = bool(re.search(r"inLibrary\s*:\s*true", query)) or (
            variables.get("inLibrary") is True
        )
        nodes = [
            self._manga_node(m, library=self._in_library(m))
            for m in self.state["mangas"]
            if self._in_library(m) or not only_library
        ]
        return {"nodes": nodes}

    def _q_manga(self, query: str, variables: dict[str, Any]) -> dict[str, Any] | None:
        wanted = self._wanted_id(query, variables, "id")
        found = [m for m in self.state["mangas"] if m["id"] == wanted]
        if not found:
            return None
        node = self._manga_node(found[0], library=self._in_library(found[0]))
        node["updateStrategy"] = found[0].get("updateStrategy", "ALWAYS_UPDATE")
        node["categories"] = {
            "nodes": [
                {"id": c["id"], "name": c["name"], "includeInUpdate": c["includeInUpdate"]}
                for c in self.categories
                if c["id"] in found[0].get("categoryIds", [])
            ]
        }
        return node

    def _q_categories(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {"nodes": copy.deepcopy(self.categories)}

    @staticmethod
    def _patch_keys(query: str) -> set[str]:
        match = re.search(r"patch\s*:\s*\{([^}]*)\}", query)
        return set(re.findall(r"(\w+)\s*:", match.group(1))) if match else set()

    def _check_patch(self, query: str, allowed: set[str]) -> None:
        """What the adapter may change on a manga, and nothing else (EF-EXP-056)."""
        extra = self._patch_keys(query) - allowed
        if extra:
            self.violations.append(f"patch keys not allowed: {sorted(extra)}")

    def _q_updateMangaCategories(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self._check_patch(query, {"addToCategories", "removeFromCategories"})
        wanted = self._wanted_id(query, variables, "id")
        for manga in self.state["mangas"]:
            if manga["id"] == wanted:
                ids = set(manga.get("categoryIds", []))
                ids |= set(variables.get("add", []))
                ids -= set(variables.get("remove", []))
                manga["categoryIds"] = sorted(ids)
        return {"manga": {"id": wanted}}

    def _chapter_node(self, chapter: dict[str, Any]) -> dict[str, Any]:
        node = {k: v for k, v in chapter.items() if k != "cbz"}
        node["mangaId"] = next(m["id"] for m in self.state["mangas"] if chapter in m["chapters"])
        node["isDownloaded"] = chapter["id"] in self._downloaded
        return node

    def _q_chapters(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        wanted = self._wanted_id(query, variables, "mangaId")
        manga = next((m for m in self.state["mangas"] if m["id"] == wanted), None)
        chapters = manga["chapters"] if manga else []
        return {"nodes": [self._chapter_node(c) for c in chapters]}

    def _q_chapter(self, query: str, variables: dict[str, Any]) -> dict[str, Any] | None:
        wanted = self._wanted_id(query, variables, "id")
        chapter = self._chapter(wanted) if wanted is not None else None
        if chapter is None:
            return None
        self._polls[chapter["id"]] = self._polls.get(chapter["id"], 0) + 1
        if chapter["id"] in self._enqueued and self._polls[chapter["id"]] >= 2:
            self._downloaded.add(chapter["id"])  # the queue needs two looks to finish
        return self._chapter_node(chapter)

    def _q_enqueueChapterDownload(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        wanted = self._wanted_id(query, variables, "id")
        if wanted is not None and self._chapter(wanted) is not None:
            self._enqueued.add(wanted)
            self._polls[wanted] = 0
            self.claims.setdefault(wanted, set()).add(self._account)
        return {"downloadStatus": {"state": "STARTED"}}

    def _q_dequeueChapterDownload(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        wanted = self._wanted_id(query, variables, "id")
        self._enqueued.discard(wanted)  # type: ignore[arg-type]
        return {"downloadStatus": {"state": "STOPPED"}}

    def _q_createCategory(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """`createCategory(input: {name, includeInUpdate})`, answering `category { id name }`."""
        match = re.search(r"createCategory\s*\(\s*input\s*:\s*\{([^}]*)\}", query)
        keys = set(re.findall(r"(\w+)\s*:", match.group(1))) if match else set()
        if keys - {"name"}:  # the category is created plain, then excluded from the download
            self.violations.append(f"createCategory keys not allowed: {sorted(keys)}")
        name = re.search(r'name\s*:\s*"([^"]*)"', query)
        wanted = name.group(1) if name else str(variables.get("name", ""))
        if any(c["name"] == wanted for c in self.categories):
            self.violations.append(f"category created twice: {wanted!r}")
        ident = self.add_category(wanted)
        return {"category": {"id": ident, "name": wanted}}

    def _q_updateCategory(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """`updateCategory(input: {id, patch})`: only `includeInDownload` may be written, and
        never `includeInUpdate` (it would stop the refresh of every followed series)."""
        self._check_patch(query, {"includeInDownload"})
        wanted = self._wanted_id(query, variables, "id")
        flag = re.search(r"includeInDownload\s*:\s*([A-Z]+)", query)
        for category in self.categories:
            if category["id"] == wanted and flag:
                category["includeInDownload"] = flag.group(1)
        return {"category": {"id": wanted}}

    def _q_deleteDownloadedChapter(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Removes the downloaded file only (the chapter stays known): upstream erasure."""
        wanted = self._wanted_id(query, variables, "id")
        self._enqueued.discard(wanted)  # type: ignore[arg-type]
        self.deleted.append(wanted)  # type: ignore[arg-type]
        holders = self.claims.get(wanted, set())  # type: ignore[arg-type]
        holders.discard(self._account)  # the reference of the account that asks, only
        if not holders and wanted in self._downloaded:
            self._downloaded.discard(wanted)  # type: ignore[arg-type]
            self.files_removed.append(wanted)  # type: ignore[arg-type]
        return {"chapters": {"id": wanted}}

    def _q_updateManga(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self._check_patch(query, {"inLibrary", "updateStrategy"})
        if variables.get("strategy", "ALWAYS_UPDATE") not in ("ALWAYS_UPDATE", "ONLY_FETCH_ONCE"):
            self.violations.append(f"unknown update strategy {variables['strategy']!r}")
        wanted = self._wanted_id(query, variables, "id")
        for manga in self.state["mangas"]:
            if manga["id"] == wanted:
                if "strategy" in variables:
                    manga["updateStrategy"] = str(variables["strategy"])
                elif self.auth_mode == "ui_login" and self._account:
                    held = self.account_library(self._account)
                    (held.add if variables.get("inLibrary", True) else held.discard)(manga["url"])
                else:
                    manga["inLibrary"] = bool(variables.get("inLibrary", True))
        return {"manga": {"id": wanted}}

    # -- as a transport, to exercise the real IntegrationFetcher ----------------------------
    def transport_handler(self, request: httpx2.Request) -> httpx2.Response:
        reply = self.handle(
            request.method, str(request.url), dict(request.headers), request.content or b""
        )
        return httpx2.Response(reply.status, headers=reply.headers, content=reply.body)

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.transport_handler)


class FakeSuwayomiFetcher:
    """The SDK `Fetcher` seen by the adapter, wired to the simulated server and nothing else.

    Same surface as `shelfhold.sdk.testing.fakes.FakeFetcher`, so the conformance kit can run on
    it: `record_mode` is `none` and `unscripted` lists everything outside the allow-list.
    """

    record_mode = "none"

    def __init__(self, server: FakeSuwayomi, base_url: str = BASE_URL) -> None:
        self.server = server
        self.base_url = base_url
        self.posts: list[dict[str, Any]] = []  # what the adapter passed to `post`
        self.downloads: list[ImageRequest] = []

    @property
    def unscripted(self) -> list[str]:
        return list(self.server.violations)

    async def aclose(self) -> None:
        """The host's `GuardedFetcher` is closed after a probe; nothing to release here."""

    def _target(self, url: str) -> str:
        if not url.startswith(self.base_url + "/"):
            self.server.violations.append(f"request outside the declared target: {url}")
            raise OSError(f"{url} is outside the declared target (record_mode=none)")
        return url[len(self.base_url) :]

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
        cache_ttl: int | None = None,
    ) -> Response:
        return self.server.handle("GET", self._target(url), headers)

    async def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        self.posts.append({"url": url, "data": data, "json": json})
        merged = dict(headers or {})
        body = b""
        if json is not None:
            merged.setdefault("Content-Type", "application/json")
            body = _dumps(json)
        elif data is not None:
            merged.setdefault("Content-Type", "application/x-www-form-urlencoded")
            body = "&".join(f"{k}={v}" for k, v in data.items()).encode()
        if self.server.refresh_delay is not None and b"refreshToken(" in body:
            await self.server.refresh_delay()  # lets a second caller reach the same refresh
        return self.server.handle("POST", self._target(url), merged, body)

    async def download(self, req: ImageRequest) -> DownloadedFile:
        self.downloads.append(req)
        reply = self.server.handle("GET", self._target(req.url), req.headers)
        if reply.status != 200:
            raise OSError(f"download answered {reply.status}")
        return DownloadedFile(
            sha256=hashlib.sha256(reply.body).hexdigest(), size=len(reply.body), content=reply.body
        )

    def cookies(self) -> CookieJarView:
        return CookieJarView()


def _dumps(value: Any) -> bytes:
    return json.dumps(value).encode()
