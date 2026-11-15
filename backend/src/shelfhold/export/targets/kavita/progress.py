"""Kavita's side of the progress synchronisation (M4 L14b, EF-EXP-026, D8, D11).

Two things, kept apart on purpose:

* the **structure** (which chapter holds which Shelfhold file) is read with the dedicated key of the
  target, `series-detail` of each series that holds a file of the destination: ids, page counts and
  paths, nothing of any account;
* the **progress** of a member is read and written with that member's own key and nothing else
  (`MemberKavitaClient`, the closed list `MEMBER_ROUTES`): `GET /api/Reader/get-progress` and
  `POST /api/Reader/progress`, the calls a Kavita client or an OPDS-PSE client makes. A
  `ProgressDto` is `volumeId`, `chapterId`, `pageNum`, `seriesId`, `libraryId` (read in the source
  of Kavita, tag v0.9.1.4; `docs/reference/progress-sync.md`).

A member's pass uses **one** client: one fetcher, so one connection pool, on one event loop kept for
the pass (`_Session`) and closed with it. Failures are sorted into the three
things the synchronisation reacts to: the key is refused (401, or 403 on `GET /api/Account`), Kavita
cannot be asked now (no answer, 429 and 5xx, retried with a cap), or this one chapter is not
available to the account (403, 404, an answer that is not a progress): skipped.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.export.domain.path_map import PathEntry, PathMap, PathMode, clean_absolute
from shelfhold.export.targets.kavita.accounts import (
    GET_PROGRESS,
    POST_PROGRESS,
    SERIES_PROGRESS,
    MemberKavitaClient,
    MemberKey,
    run_sync,
)
from shelfhold.export.targets.kavita.client import (
    MAX_SERIES_PAGES,
    SERIES_PAGE,
    CallResult,
    KavitaClient,
)
from shelfhold.export.targets.kavita.dto import library_filter
from shelfhold.export.targets.kavita.errors import ProtocolError, TargetError
from shelfhold.infrastructure.schema import export
from shelfhold.infrastructure.secrets.errors import SecretsError
from shelfhold.reading.application.member_keys import MemberKeyRevokedError
from shelfhold.reading.application.sync import (
    ChapterRef,
    ChapterRejected,
    RemotePosition,
    RemoteUnavailable,
    TokenRefused,
    TokenUnreadable,
)

log = structlog.get_logger(__name__)

__all__ = ["KavitaProgress", "MemberProgress"]

MAX_ATTEMPTS: Final = 3  # tries of one call, the first included
BASE_WAIT_S: Final = 1.0  # first wait after a failure; doubled each time
MAX_WAIT_S: Final = 30.0  # no wait is longer, whatever `Retry-After` asked
STRUCTURE_TTL_S: Final = 1800.0  # the structure is read again after this long, or when files change
_HOLDING: Final = ("written", "scanned", "resolved")  # a record whose file is in the library
MAX_STAMP_PAGES: Final = MAX_SERIES_PAGES  # of 100 series: a bigger list is not read


class _Client(Protocol):
    async def call(
        self, method: str, path: str, *, query: Any = None, body: Any = None
    ) -> CallResult: ...

    async def whoami(self) -> Any: ...


class MemberProgress:
    """One member's key against `get-progress` and `progress`, with a bounded retry."""

    def __init__(
        self,
        client: _Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
        max_wait_s: float = MAX_WAIT_S,
        run: Callable[[Coroutine[Any, Any, Any]], Any] = run_sync,
        close: Callable[[], None] = lambda: None,
    ) -> None:
        self._client = client
        self._run = run
        self._close = close
        self._sleep = sleep
        self._attempts = max_attempts
        self._max_wait = max_wait_s

    def __repr__(self) -> str:
        return "MemberProgress(<redacted>)"

    def _retrying(self, make: Callable[[], Any], *, on_forbidden: type[Exception]) -> Any:
        for attempt in range(1, self._attempts + 1):
            try:
                return self._run(make())
            except ProtocolError as error:
                status = error.status
                if status == 401:
                    raise TokenRefused from None
                if status == 403:
                    raise on_forbidden from None
                transient = status is None or status in (408, 429) or status >= 500
                if not transient:
                    raise ChapterRejected from None
                if attempt == self._attempts:
                    log.warning("progress_sync.kavita_gave_up", status=status, attempts=attempt)
                    raise RemoteUnavailable from None
                asked = error.retry_after
                wait = BASE_WAIT_S * 2 ** (attempt - 1) if asked is None else asked
                self._sleep(float(min(wait, self._max_wait)))
        raise RemoteUnavailable  # pragma: no cover  (the loop always returns or raises)

    def close(self) -> None:
        """End the pass: the connection and the loop it ran on."""
        self._close()

    def series_stamps(self, library_ids: Sequence[int]) -> dict[int, str]:
        """What the account's own series list says of each series (pages read, last read date): one
        read per hundred series. Empty when it cannot be read whole: then every chapter is asked."""
        stamps: dict[int, str] = {}
        for library in sorted(set(library_ids)):
            body = library_filter(int(library))
            for number in range(1, MAX_STAMP_PAGES + 1):
                result: CallResult = self._retrying(
                    lambda number=number, body=body: self._client.call(  # type: ignore[misc]
                        SERIES_PROGRESS.method,
                        SERIES_PROGRESS.path,
                        query={"PageNumber": number, "PageSize": SERIES_PAGE},
                        body=body,
                    ),
                    on_forbidden=ChapterRejected,
                )
                data = result.data
                if not isinstance(data, list):
                    raise ChapterRejected
                for item in data:
                    if isinstance(item, dict) and isinstance(item.get("id"), int):
                        stamps[item["id"]] = f"{item.get('pagesRead')}|{item.get('latestReadDate')}"
                if len(data) < SERIES_PAGE:
                    break
            else:
                return {}  # more series than the cap: never a silently cut list
        return stamps

    def account_name(self) -> str:
        who = self._retrying(lambda: self._client.whoami(), on_forbidden=TokenRefused)
        return str(who.username)

    def get(self, chapter: ChapterRef) -> RemotePosition:
        result: CallResult = self._retrying(
            lambda: self._client.call(
                GET_PROGRESS.method, GET_PROGRESS.path, query={"chapterId": chapter.chapter_id}
            ),
            on_forbidden=ChapterRejected,
        )
        data = result.data
        page = data.get("pageNum") if isinstance(data, dict) else None
        if not isinstance(page, int) or isinstance(page, bool) or page < 0:
            raise ChapterRejected  # Kavita did not answer a progress
        modified = data.get("lastModifiedUtc")
        return RemotePosition(page, modified if isinstance(modified, str) else None)

    def save(self, chapter: ChapterRef, page: int) -> None:
        body = {
            "volumeId": chapter.volume_id, "chapterId": chapter.chapter_id, "pageNum": page,
            "seriesId": chapter.series_id, "libraryId": chapter.library_id,
        }  # fmt: skip
        self._retrying(
            lambda: self._client.call(POST_PROGRESS.method, POST_PROGRESS.path, body=body),
            on_forbidden=ChapterRejected,
        )


class _Session:
    """The event loop of one member's pass, in a thread of its own, and the one client that runs on
    it: its connections are reused from call to call and closed with the pass."""

    def __init__(self, fetcher: Any, url: str, key: MemberKey) -> None:
        self._fetcher = fetcher
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._member = MemberKavitaClient(fetcher, url, key)

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def call(
        self, method: str, path: str, *, query: Any = None, body: Any = None
    ) -> CallResult:
        return await self._member.call(method, path, query=query, body=body)

    async def whoami(self) -> Any:
        return await self._member.whoami()

    def close(self) -> None:
        closer = getattr(self._fetcher, "aclose", None)
        if closer is not None:
            try:
                self.run(closer())
            except Exception:
                log.debug("progress_sync.close_failed")
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._loop.is_running():
            self._loop.close()

    def __repr__(self) -> str:
        return "_Session(<redacted>)"


class KavitaProgress:
    """The `ProgressRemote` of a Kavita target.

    `admin_client(target_id)` is the client of the dedicated key (structure only);
    `fetcher_for(url)` the guarded fetcher of the target; `open_token(user_id, destination_id)`
    the one reader of a member's key (`DestinationAccounts.open_token`: `None` when revoked).
    """

    def __init__(
        self,
        engine: Engine,
        *,
        admin_client: Callable[[str], KavitaClient],
        fetcher_for: Callable[..., Any],
        open_token: Callable[[str, str], Any],
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
        max_wait_s: float = MAX_WAIT_S,
        structure_ttl_s: float = STRUCTURE_TTL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._structure_ttl = structure_ttl_s
        self._monotonic = monotonic
        self._structures: dict[str, tuple[float, frozenset[tuple[str, str]], list[ChapterRef]]] = {}
        self._engine = engine
        self._admin_client = admin_client
        self._fetcher_for = fetcher_for
        self._open_token = open_token
        self._sleep = sleep
        self._attempts = max_attempts
        self._max_wait = max_wait_s

    def __repr__(self) -> str:
        return "KavitaProgress()"

    # -- structure, with the dedicated key ---------------------------------------------------
    def chapters(self, destination_id: str) -> list[ChapterRef]:
        with self._engine.connect() as connection:
            where = self._where(connection, destination_id)
            library = self._library_ref(connection, destination_id)
            records = connection.execute(
                sa.select(export.export_record.c.rel_path, export.export_record.c.export_item_id)
                .where(
                    export.export_record.c.shelf_destination_id == destination_id,
                    export.export_record.c.locator_type == "path",
                    export.export_record.c.state.in_(_HOLDING),
                )
                .order_by(export.export_record.c.rel_path)
            ).all()
        if where is None or library is None or not records:
            return []
        target_id, path_map, root_local = where
        wanted = {
            path_map.to_remote(f"{root_local}/{r.rel_path}"): str(r.export_item_id) for r in records
        }
        key = frozenset(wanted.items())
        cached = self._structures.get(destination_id)
        now = self._monotonic()
        if cached is not None and cached[1] == key and now - cached[0] < self._structure_ttl:
            return list(cached[2])  # the same files, read lately: `all-v2` and `series-detail` rest
        try:
            found = run_sync(self._structure(target_id, library, wanted))
            self._structures[destination_id] = (now, key, found)
            return list(found)
        except TargetError:  # unreachable, asleep, paused, refused: Kavita cannot be asked now
            raise RemoteUnavailable from None

    @staticmethod
    def _where(connection: sa.Connection, destination_id: str) -> tuple[str, PathMap, str] | None:
        """The target, its path map and where the destination's tree starts on this machine (the
        first writable entry, then the destination's own sub-folder): the folders of Kavita are
        the same paths seen through the map."""
        table, target = export.shelf_destination, export.export_target
        row = connection.execute(
            sa.select(table.c.target_id, table.c.root_subpath, target.c.path_map)
            .join(target, target.c.id == table.c.target_id)
            .where(table.c.id == destination_id)
        ).first()
        if row is None or not isinstance(row.path_map, list):
            return None
        path_map = PathMap(
            tuple(
                PathEntry(e["local"], e["remote"], PathMode(e.get("mode", "rw")))
                for e in row.path_map
            )
        )
        writable = path_map.writable()
        if not writable:
            return None
        base = writable[0].local
        root = clean_absolute(f"{base}/{row.root_subpath}") if row.root_subpath else base
        return str(row.target_id), path_map, root

    @staticmethod
    def _library_ref(connection: sa.Connection, destination_id: str) -> int | None:
        link, library = export.shelf_destination_remote_library, export.remote_library
        ref = connection.execute(
            sa.select(library.c.remote_ref)
            .join(link, link.c.remote_library_id == library.c.id)
            .where(link.c.shelf_destination_id == destination_id)
            .order_by(library.c.id)
        ).scalar()
        return int(ref) if isinstance(ref, str) and ref.isdigit() else None

    async def _structure(
        self, target_id: str, library_id: int, wanted: dict[str, str]
    ) -> list[ChapterRef]:
        client = self._admin_client(target_id)
        folders = {path.rsplit("/", 1)[0] for path in wanted}
        found: list[ChapterRef] = []
        for series in await client.series_of_library(library_id):
            if not any(series.lives_in(folder) for folder in folders):
                continue
            for chapter in await client.series_chapter_info(series.id):
                carriers = {wanted[p] for p in chapter.paths if p in wanted}
                if len(chapter.paths) != 1 or len(carriers) != 1:
                    continue  # a chapter of several files, or not ours: never guessed
                if chapter.paths[0].lower().endswith(".epub"):
                    continue  # Kavita counts an EPUB in book documents, not in pages of ours
                found.append(
                    ChapterRef(
                        chapter.chapter_id, chapter.volume_id, series.id, series.library_id,
                        chapter.pages, next(iter(carriers)),
                    )
                )  # fmt: skip
        return found

    # -- progress, with the member's own key -------------------------------------------------
    def member(self, user_id: str, destination_id: str) -> MemberProgress:
        try:
            token = self._open_token(user_id, destination_id)
        except SecretsError:
            raise TokenUnreadable from None
        if token is None:
            raise MemberKeyRevokedError
        url = self._url(destination_id)
        session = _Session(self._fetcher_for(url), url, MemberKey(token.reveal()))
        return MemberProgress(
            session,
            sleep=self._sleep,
            max_attempts=self._attempts,
            max_wait_s=self._max_wait,
            run=session.run,
            close=session.close,
        )

    def _url(self, destination_id: str) -> str:
        table, target = export.shelf_destination, export.export_target
        with self._engine.connect() as connection:
            config = connection.execute(
                sa.select(target.c.config)
                .join(table, table.c.target_id == target.c.id)
                .where(table.c.id == destination_id)
            ).scalar()
        url = config.get("url") if isinstance(config, dict) else None
        if not isinstance(url, str) or not url:
            raise TokenUnreadable  # no address to send a key to: nothing is sent
        return url
