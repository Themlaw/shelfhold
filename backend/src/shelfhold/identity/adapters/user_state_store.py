"""SQL store of the per-user state (`user_state`) and the title languages (`title_lang_pref`).

`merge` reads the row and writes the merged one in one `BEGIN IMMEDIATE` transaction: two writers
of the same row take turns, and the second one merges against what the first one stamped.
"""

from collections.abc import Callable, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.catalog.public import RatingService, ceiling_of
from shelfhold.identity.application.user_state import UserStateService
from shelfhold.identity.domain.user_state import UserFlags
from shelfhold.infrastructure.clock import Clock, Stamped
from shelfhold.infrastructure.schema import catalog, identity

_T = identity.user_state


def _stamped(row: Sequence[Any]) -> Stamped[UserFlags]:
    return Stamped(UserFlags(bool(row[0]), bool(row[1]), row[2]), row[3])


_COLUMNS = (_T.c.followed, _T.c.favorite, _T.c.read_at, _T.c.stamp)


class SqlUserStateStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def merge(
        self,
        user_id: str,
        work_id: str,
        apply: Callable[[Stamped[UserFlags] | None], Stamped[UserFlags]],
    ) -> Stamped[UserFlags]:
        immediate = self._engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
        with immediate.begin() as conn:
            where = (_T.c.user_id == user_id, _T.c.work_id == work_id)
            row = conn.execute(sa.select(*_COLUMNS).where(*where)).first()
            current = _stamped(row) if row is not None else None
            merged = apply(current)
            if current is not None and merged is current:  # the stored write is later: keep it
                return current
            values = {
                "followed": merged.value.followed,
                "favorite": merged.value.favorite,
                "read_at": merged.value.read_at,
                "stamp": merged.received_at,
            }
            if current is None:
                conn.execute(sa.insert(_T).values(user_id=user_id, work_id=work_id, **values))
            else:
                conn.execute(sa.update(_T).where(*where).values(**values))
            return merged

    def get(self, user_id: str, work_id: str) -> Stamped[UserFlags] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(*_COLUMNS).where(_T.c.user_id == user_id, _T.c.work_id == work_id)
            ).first()
        return _stamped(row) if row is not None else None

    def items(self, user_id: str) -> Sequence[tuple[str, Stamped[UserFlags]]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(_T.c.work_id, *_COLUMNS)
                .where(_T.c.user_id == user_id)
                .order_by(_T.c.work_id)
            ).all()
        return [(r[0], _stamped(r[1:])) for r in rows]

    def languages(self, user_id: str) -> list[str]:
        with self._engine.connect() as conn:
            raw = conn.execute(
                sa.select(identity.user.c.title_lang_pref).where(identity.user.c.id == user_id)
            ).scalar()
        return [x for x in raw if isinstance(x, str)] if isinstance(raw, list) else []

    def set_languages(self, user_id: str, languages: list[str]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(identity.user)
                .where(identity.user.c.id == user_id)
                .values(title_lang_pref=languages)
            )


_ADULT_ROLES = frozenset({"admin", "member"})


def user_state_service(engine: Engine, clock: Clock) -> UserStateService:
    rating = RatingService()

    def admits(user_id: str, work_id: str) -> bool:
        """Closed by default: only what is known to be visible is visible."""
        with engine.connect() as conn:
            user = conn.execute(
                sa.select(identity.user.c.role, identity.user.c.max_age_rating).where(
                    identity.user.c.id == user_id
                )
            ).first()
            exists = conn.execute(
                sa.select(catalog.work.c.id).where(catalog.work.c.id == work_id)
            ).first()
            if user is None or exists is None:
                return False
            if user.role in _ADULT_ROLES:
                return True
            ceiling = ceiling_of(user.max_age_rating) if user.role == "child" else None
            age = rating.effective_age_of(conn, work_id)
            return ceiling is not None and age is not None and age.allowed_under(ceiling)

    def titles_of(work_id: str) -> tuple[str, list[tuple[str, str, str]]]:
        with engine.connect() as conn:
            own = conn.execute(
                sa.select(catalog.work.c.title).where(catalog.work.c.id == work_id)
            ).scalar()
            rows = conn.execute(
                sa.select(
                    catalog.work_title.c.lang, catalog.work_title.c.kind, catalog.work_title.c.title
                ).where(catalog.work_title.c.work_id == work_id)
            ).all()
        return str(own), [(r[0], r[1], r[2]) for r in rows]

    return UserStateService(SqlUserStateStore(engine), clock, admits, titles_of)
