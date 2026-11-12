"""Per-user state: follow, favourite, read, and the title languages (EF-USR-007, EF-BIB-022).

One rule above the rest (IDOR, ENF-SEC-010): the user is always the caller's own, taken from the
session and never from a parameter. Keys and device tokens never come here. A Work the caller may
not see (above a child's ceiling, unrated for a child, or absent) is refused with the one answer
for an absent Work, so that a mark cannot confirm that something exists.

Concurrent writes are merged by the server's clock (`merge_last_write_wins`, ENF-TIME-001): the
instant that counts is the one the server takes on receipt. A client's own timestamp is only
checked (a claim from beyond the tolerated drift is refused), never used to order writes.
"""

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from shelfhold.identity.domain.credentials import Credential, Principal
from shelfhold.identity.domain.user_state import (
    UserFlags,
    UserStateError,
    normalise_languages,
    pick_title,
)
from shelfhold.infrastructure.clock import (
    Clock,
    FutureDateError,
    Stamped,
    merge_last_write_wins,
)

NOT_FOUND = "not_found"  # absent, above the ceiling and unrated-for-a-child look the same

# "Which of these Works may this user see?": answered by the composition root, who knows the
# catalogue, with the one filter of L09 (a condition of the query, never a loop after it).
Admits = Callable[[str, Collection[str]], set[str]]
# The Work's own title and its `(lang, kind, title)` rows: also the catalog's, read after `Admits`.
TitlesOf = Callable[[str], tuple[str, Sequence[tuple[str, str, str]]]]


@dataclass(frozen=True)
class WorkState:
    work_id: str
    flags: Stamped[UserFlags] | None  # None: the user never touched this Work

    @property
    def stamp(self) -> datetime | None:
        return self.flags.received_at if self.flags else None


@dataclass(frozen=True)
class TitleChoice:
    work_id: str
    title: str
    lang: str | None


class UserStateStore(Protocol):
    def merge(
        self,
        user_id: str,
        work_id: str,
        apply: Callable[[Stamped[UserFlags] | None], Stamped[UserFlags]],
    ) -> Stamped[UserFlags]: ...

    def get(self, user_id: str, work_id: str) -> Stamped[UserFlags] | None: ...

    def items(self, user_id: str) -> Sequence[tuple[str, Stamped[UserFlags]]]: ...

    def languages(self, user_id: str) -> list[str]: ...

    def set_languages(self, user_id: str, languages: list[str]) -> None: ...


class UserStateService:
    def __init__(
        self, store: UserStateStore, clock: Clock, admits: Admits, titles_of: TitlesOf
    ) -> None:
        self._store = store
        self._titles_of = titles_of
        self._clock = clock
        self._admits = admits

    @staticmethod
    def _owner(principal: Principal) -> str:
        if principal.credential is not Credential.SESSION:
            raise UserStateError("not_allowed")  # a key or a token is a program, not a person
        return principal.user_id

    def _visible(self, user_id: str, work_id: str) -> None:
        if work_id not in self._admits(user_id, [work_id]):
            raise UserStateError(NOT_FOUND)

    def get(self, principal: Principal, work_id: str) -> WorkState:
        user_id = self._owner(principal)
        self._visible(user_id, work_id)
        return WorkState(work_id, self._store.get(user_id, work_id))

    def items(self, principal: Principal) -> list[WorkState]:
        user_id = self._owner(principal)
        stored = self._store.items(user_id)
        admitted = self._admits(user_id, [work_id for work_id, _ in stored])
        return [WorkState(work_id, flags) for work_id, flags in stored if work_id in admitted]

    def set(
        self,
        principal: Principal,
        work_id: str,
        *,
        followed: bool | None = None,
        favorite: bool | None = None,
        read: bool | None = None,
        client_ts: datetime | None = None,
    ) -> WorkState:
        user_id = self._owner(principal)
        self._visible(user_id, work_id)

        def apply(current: Stamped[UserFlags] | None) -> Stamped[UserFlags]:
            base = current.value if current else UserFlags()
            now = self._clock.now()
            flags = replace(
                base,
                followed=base.followed if followed is None else followed,
                favorite=base.favorite if favorite is None else favorite,
                read_at=base.read_at if read is None else (now if read else None),
            )
            try:
                return merge_last_write_wins(current, flags, clock=self._clock, client_ts=client_ts)
            except FutureDateError:
                raise UserStateError("future_timestamp") from None
            except ValueError:
                raise UserStateError("naive_timestamp") from None

        return WorkState(work_id, self._store.merge(user_id, work_id, apply))

    def languages(self, principal: Principal) -> list[str]:
        return self._store.languages(self._owner(principal))

    def set_languages(self, principal: Principal, raw: object) -> list[str]:
        user_id = self._owner(principal)
        languages = normalise_languages(raw)
        self._store.set_languages(user_id, languages)
        return languages

    def title(self, principal: Principal, work_id: str) -> TitleChoice:
        """The title in the caller's language, of a Work the caller may see: the visibility
        check comes first, so a hidden Work yields no title in any language."""
        user_id = self._owner(principal)
        self._visible(user_id, work_id)
        own, titles = self._titles_of(work_id)
        title, lang = pick_title(self._store.languages(user_id), own, titles)
        return TitleChoice(work_id, title, lang)
