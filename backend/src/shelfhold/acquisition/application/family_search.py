"""The family search of a child profile (EF-USR-014, EF-USR-018, M3 L19): the reduced form of the
federated search.

It is the same search (one query, the sources together, opaque cursors, grouping by append only)
over a narrower set and with a narrower answer:

- **Which sources**: only the ones the caller gives, which are the sources marked "family" that the
  child's policy leaves open. A cursor that names another source is not followed: the walk keeps
  only the sources asked now.
- **What is shown of a result**: a title, the one action "request", and the opaque reference the
  request needs (source key and remote id). No cover, no summary, no format, no count, no trust
  card, no name of the source, no error of a source: a failing source answers like one with nothing.
- **Dropped results leave no trace**: a result the `keep` filter refuses (the subject and word
  filter) never enters the walk, so it is in no page, no count and no cursor.
- **Modes**: in "hide" mode (`hidden`) or for a reader the rules do not know (`closed`), no source
  is asked at all and the answer is the empty one. The cursors of the family search are kept
  apart from the adult's, so an adult's walk cannot be continued as a child's.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from shelfhold.acquisition.application.federated_search import (
    FederatedCursors,
    FederatedSearch,
    FederatedSource,
    Searcher,
)
from shelfhold.sdk.refs import SeriesRef

__all__ = ["FamilyPage", "FamilyResult", "FamilySearch", "empty_page"]


@dataclass(frozen=True)
class FamilyResult:
    """What a child sees of a remote result: nothing but a title and what a request needs."""

    title: str
    source_key: str
    remote_id: str
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
        *,
        keep: Callable[[SeriesRef], bool],
        timeout_s: float = 20.0,
    ) -> None:
        self._inner = FederatedSearch(searcher, cursors, timeout_s=timeout_s, keep=keep)

    async def search(
        self,
        sources: Sequence[FederatedSource],
        query: str,
        *,
        owner: str,
        cursor: str | None = None,
    ) -> FamilyPage:
        """The first page, or with its `cursor` the next one. With no source to ask, the empty
        page (a cursor is then still checked: a forged one is refused, not ignored)."""
        page = await self._inner.search(sources, query, owner=owner, cursor=cursor)
        found: list[FamilyResult] = []
        for group in page.items:
            if group.appended or not group.editions:
                continue  # shown before: only the Editions that are new are named again
            first = group.editions[0]
            found.append(FamilyResult(group.title, first.source_key, first.remote_id))
        return FamilyPage(tuple(found), "results" if found else "empty", page.next_cursor)
