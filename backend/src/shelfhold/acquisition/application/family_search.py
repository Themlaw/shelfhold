"""The family search of a child profile (EF-USR-014, EF-USR-018, M3 L19): the reduced form of the
federated search.

It is the same search (one query, the sources together, opaque cursors, grouping by append only)
over a narrower set and with a narrower answer:

- **Which sources**: only the ones the caller gives (marked "family", not self-declared adult, left
  open by the child's policy). A cursor that names another source is not followed: the walk keeps
  only the sources asked now.
- **What is shown of a result**: a title and the one action "request", with a reference that means
  nothing outside this host (`FamilyRefs`: a random token, bound to the child, that the host
  resolves to the source, the remote id and the memo). No source key, no remote id, no cover, no
  summary, no format, no count, no trust card, no error of a source.
- **Dropped results leave no trace**: a result the `keep` filter refuses never enters the walk, so
  it is in no page, no count and no cursor. A page with nothing kept has no cursor either: the
  answer is the one of a search that found nothing.
- **Modes**: the caller decides the mode and the sources before asking (in "hide" mode, or for a
  reader the rules do not know, it asks nothing and answers `empty_page()`; a cursor is not
  looked at then). The cursors of the family search are kept apart from the adult's.
"""

from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from shelfhold.acquisition.application.federated_search import (
    FederatedCursors,
    FederatedSearch,
    FederatedSource,
    Searcher,
)
from shelfhold.sdk.refs import SeriesRef

__all__ = [
    "FamilyPage",
    "FamilyRef",
    "FamilyRefs",
    "FamilyResult",
    "FamilySearch",
    "InvalidFamilyRefError",
    "empty_page",
]

REF_TTL: Final = timedelta(hours=1)
MAX_REFS: Final = 2048


class InvalidFamilyRefError(Exception):
    """A reference that is forged, another child's, or expired. One code, whatever the reason."""

    code = "invalid_ref"


@dataclass(frozen=True)
class FamilyRef:
    """What a reference stands for: the result a child may ask for."""

    source_key: str
    remote_id: str
    memo: dict[str, Any] = field(default_factory=dict)
    title: str = ""  # what the child was shown: the title of her request (L35)


@dataclass(frozen=True)
class _Held:
    expires: datetime
    owner: str
    ref: FamilyRef


class FamilyRefs:
    """The references of the results shown to children, in memory: bounded, expiring, bound to
    the child they were shown to. Nothing in a token tells what it stands for."""

    def __init__(self, now: Callable[[], datetime], *, ttl: timedelta = REF_TTL) -> None:
        self._now = now
        self._ttl = ttl
        self._held: OrderedDict[str, _Held] = OrderedDict()
        self._lock = threading.Lock()

    def issue(self, owner: str, ref: FamilyRef) -> str:
        token = secrets.token_urlsafe(18)
        with self._lock:
            self._expire()
            self._held[token] = _Held(self._now() + self._ttl, owner, ref)
            while len(self._held) > MAX_REFS:
                self._held.popitem(last=False)
        return token

    def resolve(self, token: str, *, owner: str) -> FamilyRef:
        with self._lock:
            self._expire()
            held = self._held.get(token) if len(token) <= 64 else None
            if held is None or not secrets.compare_digest(held.owner.encode(), owner.encode()):
                raise InvalidFamilyRefError
            return held.ref

    def _expire(self) -> None:
        now = self._now()
        for token in [t for t, h in self._held.items() if h.expires <= now]:
            del self._held[token]


@dataclass(frozen=True)
class FamilyResult:
    """What a child sees of a remote result: a title and what a request needs, by reference."""

    title: str
    request_ref: str
    action: Literal["request"] = "request"


@dataclass(frozen=True)
class FamilyPage:
    items: tuple[FamilyResult, ...]
    state: Literal["results", "empty"]
    next_cursor: str | None


def empty_page() -> FamilyPage:
    return FamilyPage((), "empty", None)


class FamilySearch:
    def __init__(
        self,
        searcher: Searcher,
        cursors: FederatedCursors,
        refs: FamilyRefs,
        *,
        keep: Callable[[SeriesRef], bool],
        title_of: Callable[[str], str] = lambda title: title,
        timeout_s: float = 20.0,
    ) -> None:
        self._inner = FederatedSearch(searcher, cursors, timeout_s=timeout_s, keep=keep)
        self._refs = refs
        self._title_of = title_of

    async def search(
        self,
        sources: Sequence[FederatedSource],
        query: str,
        *,
        owner: str,
        cursor: str | None = None,
    ) -> FamilyPage:
        """The first page, or with its `cursor` the next one. A cursor that is not this child's,
        for this query, is refused (`InvalidFederatedCursorError`), also when no source is open."""
        page = await self._inner.search(sources, query, owner=owner, cursor=cursor)
        found: list[FamilyResult] = []
        for group in page.items:
            if group.appended or not group.editions:
                continue  # shown before: only the Editions that are new are named again
            first = group.editions[0]
            title = self._title_of(group.title)
            token = self._refs.issue(
                owner, FamilyRef(first.source_key, first.remote_id, dict(first.memo), title)
            )
            found.append(FamilyResult(title, token))
        # nothing kept: no cursor, the answer of a search that found nothing
        return FamilyPage(
            tuple(found), "results" if found else "empty", page.next_cursor if found else None
        )
