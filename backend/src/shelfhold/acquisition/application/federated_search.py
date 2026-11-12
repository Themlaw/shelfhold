"""Federated, multi-source search (EF-SRC-005, EF-UX-011).

One query goes to every source that is switched on and searches. The sources answer together and
each in its own time: one that fails, or is slow, is reported in `searched_in` and stops no other.

**Cursors.** A cursor is a random token that names a walk the host keeps in memory (the cursors the
sources handed back, the Works already served). It carries nothing: no source, no position, no
query. So it cannot be forged or edited, only copied, and a copy is useless to another person
(the walk is bound to its owner), for another query (bound to it) or later (it expires). Anything
else is `invalid_cursor`, never a guess. A restart of the host forgets the walks: a cursor then
expires, which is what a client already handles.

**Grouping.** Results are grouped by Work **by append only**: two results that share an external
identifier are one Work with two Editions; a title is never a ground. A Work is never merged with
another (a result that carries the identifiers of two Works joins the first), never moves, and keeps
its `group_id`. An Edition that arrives on a later page is named with its Work again, `appended`,
alone. The formats stay apart: each Edition says its format, and a Work lists them by format.

**Honesty.** `state` tells empty (every source answered, none had anything) from unavailable (every
source failed) from no sources at all, and the empty state never points at a site: `searched_in`
names the sources asked, by their key and name, and the action is to ask for the book.
"""

from __future__ import annotations

import asyncio
import copy
import hmac
import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Final, Protocol

import structlog

from shelfhold.sdk.models import Page
from shelfhold.sdk.refs import SeriesRef

__all__ = [
    "CURSOR_TTL",
    "FederatedCursors",
    "FederatedEdition",
    "FederatedGroup",
    "FederatedPage",
    "FederatedSearch",
    "FederatedSource",
    "InvalidFederatedCursorError",
    "SourceReport",
    "format_of",
]

log = structlog.get_logger(__name__)

CURSOR_TTL: Final = timedelta(minutes=15)
MAX_WALKS: Final = 256  # walks kept at once: a flood of searches never grows the memory
SOURCE_TIMEOUT_S: Final = 20.0
MAX_ROUNDS: Final = 3  # pages asked in one go while nothing has come back yet
MAX_ITEMS_PER_SOURCE_PAGE: Final = 200
MAX_IDS_PER_RESULT: Final = 8
MAX_ID_PROVIDER_CHARS: Final = 64
MAX_ID_VALUE_CHARS: Final = 200
MAX_TITLE_CHARS: Final = 300
MAX_TOKEN_CHARS: Final = 128
_CODE = re.compile(r"[a-z][a-z0-9_.]{0,63}")
_ERROR: Final = "source.error"
_TIMEOUT: Final = "source.timeout"


class InvalidFederatedCursorError(Exception):
    """A cursor that is forged, foreign, spent or expired. One code, whatever the reason."""

    code = "invalid_cursor"


class Searcher(Protocol):
    """The one door to a source: `NativeAcquirer.search`."""

    async def search(
        self, source_key: str, query: str, *, cursor: str | None = None
    ) -> Page[SeriesRef]: ...


def format_of(kinds: Collection[str]) -> str:
    """The format a source gives, from the content kinds its manifest declares."""
    if not kinds:
        return "unknown"
    return next(iter(kinds)) if len(kinds) == 1 else "mixed"


@dataclass(frozen=True)
class FederatedSource:
    key: str
    name: str
    format: str
    trust: Mapping[str, object] = field(default_factory=dict)  # the trust card, as it is shown


@dataclass(frozen=True)
class SourceReport:
    """One line of "Searched in...": a source that was asked, and how it answered."""

    source_key: str
    name: str
    format: str
    status: str  # "ok" | "error"
    code: str | None
    returned: int
    trust: Mapping[str, object]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key, "name": self.name, "format": self.format,
            "status": self.status, "code": self.code, "returned": self.returned,
            "trust": dict(self.trust),
        }  # fmt: skip


@dataclass(frozen=True)
class FederatedEdition:
    source_key: str
    remote_id: str
    title: str
    format: str


@dataclass(frozen=True)
class FederatedGroup:
    """A Work as the sources name it. `appended`: it was served before, these Editions are new."""

    group_id: str
    title: str
    editions: tuple[FederatedEdition, ...]
    external_ids: tuple[tuple[str, str], ...]
    appended: bool = False

    @property
    def editions_by_format(self) -> dict[str, tuple[FederatedEdition, ...]]:
        by_format: dict[str, list[FederatedEdition]] = {}
        for edition in self.editions:
            by_format.setdefault(edition.format, []).append(edition)
        return {fmt: tuple(eds) for fmt, eds in by_format.items()}


@dataclass
class FederatedPage:
    items: list[FederatedGroup]
    searched_in: list[SourceReport]
    state: str  # "results" | "empty" | "unavailable" | "no_sources"
    action: str | None  # "request" when there is nothing to show and a person may ask for it
    next_cursor: str | None
    formats: tuple[str, ...]


# -- the walk ----------------------------------------------------------------------------------


@dataclass
class _SourceWalk:
    info: FederatedSource
    cursor: str | None = None
    done: bool = False
    status: str = "ok"
    code: str | None = None
    returned: int = 0

    def report(self) -> SourceReport:
        info = self.info
        return SourceReport(
            info.key, info.name, info.format, self.status, self.code, self.returned, info.trust
        )


@dataclass
class _Work:
    group_id: str
    title: str
    editions: list[FederatedEdition] = field(default_factory=list)
    ids: set[tuple[str, str]] = field(default_factory=set)


@dataclass
class _Walk:
    sources: dict[str, _SourceWalk]
    works: list[_Work] = field(default_factory=list)
    by_id: dict[tuple[str, str], int] = field(default_factory=dict)
    editions: set[tuple[str, str]] = field(default_factory=set)
    served: int = 0

    def add(self, source: FederatedSource, ref: SeriesRef, fresh: dict[int, int]) -> None:
        """Append one result. `fresh`: position of each Work -> editions already given before this
        page (so the page can say which are new)."""
        identity = (source.key, ref.remote_id)
        if identity in self.editions:
            return
        self.editions.add(identity)
        ids = _external_ids(ref)
        known = sorted(self.by_id[i] for i in ids if i in self.by_id)
        if known:
            position = known[0]  # the first Work: a result that bridges two merges nothing
        else:
            position = len(self.works)
            self.works.append(_Work(f"g{position + 1}", ref.title[:MAX_TITLE_CHARS]))
            fresh[position] = 0
        work = self.works[position]
        fresh.setdefault(position, len(work.editions))
        for found in ids:
            if self.by_id.setdefault(found, position) == position:
                work.ids.add(found)
        work.editions.append(
            FederatedEdition(source.key, ref.remote_id, ref.title[:MAX_TITLE_CHARS], source.format)
        )


def _external_ids(ref: SeriesRef) -> list[tuple[str, str]]:
    """The identifiers a result carries (`memo["external_ids"]`, provider -> value): bounded, and
    anything that is not plain text is ignored. Never the title, never the source's own key."""
    raw = ref.memo.get("external_ids")
    if not isinstance(raw, Mapping):
        return []
    found: list[tuple[str, str]] = []
    for provider, value in raw.items():
        if not isinstance(provider, str) or not isinstance(value, str):
            continue
        provider, value = provider.strip().casefold(), value.strip()
        if not provider or not value:
            continue
        if len(provider) > MAX_ID_PROVIDER_CHARS or len(value) > MAX_ID_VALUE_CHARS:
            continue
        found.append((provider, value))
    return sorted(set(found))[:MAX_IDS_PER_RESULT]


@dataclass
class _Held:
    expires: datetime
    owner: str
    digest: str  # of the query the walk answers
    walk: _Walk


class FederatedCursors:
    """The walks the host keeps, by cursor: bounded, expiring, bound to an owner and a query."""

    def __init__(
        self,
        now: Callable[[], datetime],
        *,
        ttl: timedelta = CURSOR_TTL,
        max_walks: int = MAX_WALKS,
    ) -> None:
        self._now = now
        self._ttl = ttl
        self._max = max_walks
        self._held: OrderedDict[str, _Held] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._held)

    def issue(self, walk: _Walk, *, owner: str, query: str) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._expire()
            self._held[token] = _Held(self._now() + self._ttl, owner, _digest(query), walk)
            while len(self._held) > self._max:
                self._held.popitem(last=False)
        return token

    def resume(self, token: str, *, owner: str, query: str) -> _Walk:
        """A copy of the walk: the cursor is not spent, a retried request gets the same page."""
        with self._lock:
            self._expire()
            held = self._held.get(token) if len(token) <= MAX_TOKEN_CHARS else None
            if (
                held is None
                or not hmac.compare_digest(held.owner.encode(), owner.encode())
                or not hmac.compare_digest(held.digest, _digest(query))
            ):
                raise InvalidFederatedCursorError
            self._held.move_to_end(token)
            return copy.deepcopy(held.walk)

    def _expire(self) -> None:
        now = self._now()
        for token in [t for t, held in self._held.items() if held.expires <= now]:
            del self._held[token]


def _digest(query: str) -> str:
    return sha256(query.encode()).hexdigest()


# -- the search --------------------------------------------------------------------------------


class _Failed:
    def __init__(self, code: str) -> None:
        self.code = code


def _code_of(error: BaseException) -> str:
    """The catalog code a source's failure carries; never its message, which may hold an address."""
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and _CODE.fullmatch(code) else _ERROR


class FederatedSearch:
    def __init__(
        self,
        searcher: Searcher,
        cursors: FederatedCursors,
        *,
        timeout_s: float = SOURCE_TIMEOUT_S,
        keep: Callable[[SeriesRef], bool] | None = None,
    ) -> None:
        self._searcher = searcher
        self._cursors = cursors
        self._timeout = timeout_s
        self._keep = keep  # a result it refuses never enters a walk: no page, count or cursor

    async def search(
        self,
        sources: Sequence[FederatedSource],
        query: str,
        *,
        owner: str,
        cursor: str | None = None,
    ) -> FederatedPage:
        """The first page of a search, or (with its `cursor`) the next one."""
        if cursor is None:
            walk = _Walk({s.key: _SourceWalk(s) for s in sources})
        else:
            walk = self._cursors.resume(cursor, owner=owner, query=query)
        live = {s.key for s in sources}
        for key, held in walk.sources.items():
            if key not in live:
                held.done = True  # switched off since: no longer asked, still listed
        before = len(walk.works)
        fresh: dict[int, int] = {}
        for _ in range(MAX_ROUNDS):
            await self._round(walk, query, fresh)
            if len(walk.works) > before or all(s.done for s in walk.sources.values()):
                break
        return self._page(walk, fresh, owner=owner, query=query)

    async def _round(self, walk: _Walk, query: str, fresh: dict[int, int]) -> None:
        asked = [(k, s) for k, s in walk.sources.items() if not s.done]
        answers = await asyncio.gather(*(self._ask(k, query, s.cursor) for k, s in asked))
        for (key, held), answer in zip(asked, answers, strict=True):  # in source order, always
            if isinstance(answer, _Failed):
                held.status, held.code, held.done = "error", answer.code, True
                log.info("federated_search.source_failed", plugin_id=key, code=answer.code)
                continue
            items = answer.items[:MAX_ITEMS_PER_SOURCE_PAGE]
            token = answer.next.token if answer.next else None
            sent, held.cursor = held.cursor, token
            held.done = token is None or token == sent  # a source that repeats itself is done
            for ref in items:
                if self._keep is not None and not self._keep(ref):
                    continue  # dropped before it is counted: it leaves no trace
                held.returned += 1
                walk.add(held.info, ref, fresh)

    async def _ask(self, key: str, query: str, cursor: str | None) -> Page[SeriesRef] | _Failed:
        try:
            async with asyncio.timeout(self._timeout):
                return await self._searcher.search(key, query, cursor=cursor)
        except TimeoutError:
            return _Failed(_TIMEOUT)
        except Exception as error:  # a source that breaks must stop no other
            return _Failed(_code_of(error))

    def _page(self, walk: _Walk, fresh: dict[int, int], *, owner: str, query: str) -> FederatedPage:
        groups = [
            FederatedGroup(
                work.group_id, work.title, tuple(work.editions[given:]), tuple(sorted(work.ids)),
                appended=given > 0,
            )
            for position, given in sorted(fresh.items())
            for work in [walk.works[position]]
        ]  # fmt: skip
        reports = [s.report() for s in walk.sources.values()]
        more = any(not s.done for s in walk.sources.values())
        token = self._cursors.issue(walk, owner=owner, query=query) if more else None
        served = walk.served
        walk.served += len(groups)
        if not reports:
            state = "no_sources"
        elif groups or served or more:
            state = "results"
        elif all(r.status == "error" for r in reports):
            state = "unavailable"
        else:
            state = "empty"
        return FederatedPage(
            items=groups, searched_in=reports, state=state,
            action="request" if state in {"empty", "no_sources"} else None,
            next_cursor=token,
            formats=tuple(sorted({e.format for g in groups for e in g.editions})),
        )  # fmt: skip
