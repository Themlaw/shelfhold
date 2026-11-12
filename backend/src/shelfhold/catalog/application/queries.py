"""The one filter of what a reader may see (EF-USR-006, M3 L09): a condition of the query.

Every surface that shows a Work, an Edition, a series or a Unit to a person asks here, with the
reader's scope (`identity.domain.visibility.ViewerScope`, handed in by the caller: this context
never reads a profile). The scope becomes a condition of the SQL itself, so that a page, a count
or a set of identifiers is made of visible rows only: nothing is filtered after the fact, and no
pagination or total can carry a hidden row out.

For a child (a ceiling, 99 being X) a Work is visible when:
- an admin rated it as a whole (`rated_at`), with an age (a merge can leave an age without a
  rating: that Work is not rated, "classer = approuver");
- its age is at most the ceiling, it is not flagged adult, and none of its Editions carries an age
  above the ceiling (the effective age is the strictest of them);
and an Edition is visible when its Work is, it was rated (an Edition that arrived after the last
rating waits in "À classer"), its own age is not above, and no open suggested age is above.

The scope is the library of the profile, never a shelf: where an Edition is filed, and who a shelf
is allowed to, change nothing here. An adult sees everything; a scope that sees nothing, nothing.
Absent and hidden are the same answer: an identifier that is not visible is simply not returned.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

import sqlalchemy as sa

from shelfhold.infrastructure.schema import catalog

__all__ = [
    "MAX_PAGE",
    "ReaderScope",
    "VisibleWork",
    "count_visible_works",
    "list_visible_works",
    "visible_edition_ids",
    "visible_editions_clause",
    "visible_series_ids",
    "visible_unit_ids",
    "visible_work_ids",
    "visible_works_clause",
    "work_is_visible",
]

MAX_PAGE = 200
_CHUNK = 500
_X = 99  # `max_age_rating` stores X as 99


class ReaderScope(Protocol):
    """What the filter needs of a scope; `identity.domain.visibility.ViewerScope` is one."""

    @property
    def kind(self) -> str: ...

    @property
    def ceiling(self) -> int | None: ...

    @property
    def unrestricted(self) -> bool: ...


@dataclass(frozen=True)
class VisibleWork:
    work_id: str
    title: str
    kind: str


def _age_within(
    age: sa.ColumnElement[int], x: sa.ColumnElement[bool], ceiling: int
) -> sa.ColumnElement[bool]:
    """`age`/`x` fit under `ceiling`; a NULL age does not fit (it was never given)."""
    if ceiling >= _X:
        return age.is_not(None)
    return sa.and_(age.is_not(None), x.is_(False), age <= ceiling)


def _above(
    age: sa.ColumnElement[int], x: sa.ColumnElement[bool], ceiling: int
) -> sa.ColumnElement[bool]:
    """An explicit age that is over `ceiling` (a NULL age says nothing)."""
    if ceiling >= _X:
        return sa.false()
    return sa.or_(x.is_(True), sa.and_(age.is_not(None), age > ceiling))


def _exceeds(
    value: sa.ColumnElement[int], x: sa.ColumnElement[bool],
    age: sa.ColumnElement[int], age_x: sa.ColumnElement[bool],
) -> sa.ColumnElement[bool]:  # fmt: skip
    """A suggested age that is stricter than the rating (EF-BIB-055: the same comparison as
    `age.admission`): X over anything that is not X, or a number over a lower number."""
    return sa.and_(
        age_x.is_(False),
        sa.or_(x.is_(True), sa.and_(age.is_not(None), value > age)),
    )


def _ceiling(scope: ReaderScope) -> int | None:
    if scope.kind == "ceiling" and isinstance(scope.ceiling, int):
        return scope.ceiling
    return None


def visible_works_clause(scope: ReaderScope) -> sa.ColumnElement[bool]:
    """The condition on `catalog.work` for this reader."""
    if scope.unrestricted:
        return sa.true()
    ceiling = _ceiling(scope)
    if ceiling is None:  # closed by default: a scope nobody defined sees nothing
        return sa.false()
    work, edition, suggestion = catalog.work.c, catalog.edition.c, catalog.age_suggestion.c
    open_work_suggestion = (
        sa.select(sa.literal(1))
        .where(
            suggestion.entity_type == "work",
            suggestion.entity_id == work.id,
            suggestion.state == "open",
            _exceeds(suggestion.value, suggestion.x, work.age_rating, work.age_x),
        )
        .exists()
    )  # the shelves block such a Work (`suggestion_above`): so does the filter
    above_edition = (
        sa.select(sa.literal(1))
        .where(edition.work_id == work.id, _above(edition.age_rating, edition.age_x, ceiling))
        .exists()
    )
    return sa.and_(
        work.rated_at.is_not(None),
        _age_within(work.age_rating, work.age_x, ceiling),
        work.is_adult.is_(False),
        ~above_edition,
        ~open_work_suggestion,
    )


def visible_editions_clause(scope: ReaderScope) -> sa.ColumnElement[bool]:
    """The condition on `catalog.edition` for this reader."""
    if scope.unrestricted:
        return sa.true()
    ceiling = _ceiling(scope)
    if ceiling is None:
        return sa.false()
    work, edition, suggestion = catalog.work.c, catalog.edition.c, catalog.age_suggestion.c
    parent = catalog.work.alias("parent_work").c
    # what the Edition counts for, by the one definition of the shelves (`age.EditionAge.age`,
    # `rating._age`): its own age when an admin fixed one (an X counts as X), else its Work's
    own = sa.or_(edition.age_x.is_(True), edition.age_rating.is_not(None))
    eff_age = sa.case((own, edition.age_rating), else_=parent.age_rating)
    eff_x = sa.case((own, edition.age_x), else_=parent.age_x).is_(True)
    waiting = (
        sa.select(sa.literal(1))
        .where(
            parent.id == edition.work_id,
            suggestion.state == "open",
            sa.or_(
                sa.and_(suggestion.entity_type == "edition", suggestion.entity_id == edition.id),
                sa.and_(suggestion.entity_type == "work", suggestion.entity_id == edition.work_id),
            ),
            _exceeds(suggestion.value, suggestion.x, eff_age, eff_x),
        )
        .exists()
    )  # a suggested age over the rating: "À classer", out of every shelf, so out of sight
    of_visible_work = edition.work_id.in_(sa.select(work.id).where(visible_works_clause(scope)))
    return sa.and_(
        edition.rated_at.is_not(None),
        sa.not_(_above(edition.age_rating, edition.age_x, ceiling)),
        ~waiting,
        of_visible_work,
    )


def _chunks(ids: Iterable[str]) -> Iterable[list[str]]:
    unique = sorted({i for i in ids if isinstance(i, str)})
    for start in range(0, len(unique), _CHUNK):
        yield unique[start : start + _CHUNK]


def visible_work_ids(
    connection: sa.Connection, scope: ReaderScope, candidates: Iterable[str]
) -> set[str]:
    """The ones of `candidates` this reader may see (an unknown identifier is not among them)."""
    found: set[str] = set()
    where = visible_works_clause(scope)
    for chunk in _chunks(candidates):
        rows = connection.execute(
            sa.select(catalog.work.c.id).where(catalog.work.c.id.in_(chunk), where)
        )
        found.update(r[0] for r in rows)
    return found


def work_is_visible(connection: sa.Connection, scope: ReaderScope, work_id: str) -> bool:
    return work_id in visible_work_ids(connection, scope, [work_id])


def visible_edition_ids(
    connection: sa.Connection, scope: ReaderScope, candidates: Iterable[str]
) -> set[str]:
    found: set[str] = set()
    where = visible_editions_clause(scope)
    for chunk in _chunks(candidates):
        rows = connection.execute(
            sa.select(catalog.edition.c.id).where(catalog.edition.c.id.in_(chunk), where)
        )
        found.update(r[0] for r in rows)
    return found


def visible_unit_ids(
    connection: sa.Connection, scope: ReaderScope, candidates: Iterable[str]
) -> set[str]:
    """A Unit is visible when its Edition is."""
    found: set[str] = set()
    unit, edition = catalog.unit.c, catalog.edition.c
    where = visible_editions_clause(scope)
    for chunk in _chunks(candidates):
        rows = connection.execute(
            sa.select(unit.id)
            .select_from(catalog.unit.join(catalog.edition, edition.id == unit.edition_id))
            .where(unit.id.in_(chunk), where)
        )
        found.update(r[0] for r in rows)
    return found


def visible_series_ids(
    connection: sa.Connection, scope: ReaderScope, candidates: Iterable[str]
) -> set[str]:
    """A series is visible when it has members and every member Work is: a title that spoils a
    hidden member never shows."""
    found: set[str] = set()
    member, work = catalog.series_member.c, catalog.work.c
    hidden_member = (
        sa.select(sa.literal(1))
        .select_from(catalog.series_member.join(catalog.work, work.id == member.work_id))
        .where(member.series_id == catalog.series.c.id, ~visible_works_clause(scope))
        .exists()
    )
    has_member = sa.select(sa.literal(1)).where(member.series_id == catalog.series.c.id).exists()
    for chunk in _chunks(candidates):
        rows = connection.execute(
            sa.select(catalog.series.c.id).where(
                catalog.series.c.id.in_(chunk), has_member, ~hidden_member
            )
        )
        found.update(r[0] for r in rows)
    return found


def count_visible_works(connection: sa.Connection, scope: ReaderScope) -> int:
    """How many Works this reader may see: the same condition as the list, so a total cannot say
    more than the pages do."""
    return int(
        connection.execute(
            sa.select(sa.func.count()).select_from(catalog.work).where(visible_works_clause(scope))
        ).scalar_one()
    )


def list_visible_works(
    connection: sa.Connection, scope: ReaderScope, *, limit: int, offset: int
) -> list[VisibleWork]:
    """A page of the Works this reader may see, in title order. A negative offset or limit is no
    page; a page is never longer than `MAX_PAGE`."""
    if limit <= 0 or offset < 0:
        return []
    work = catalog.work.c
    rows = connection.execute(
        sa.select(work.id, work.title, work.kind)
        .where(visible_works_clause(scope))
        .order_by(work.sort_title, work.id)
        .limit(min(limit, MAX_PAGE))
        .offset(offset)
    )
    return [VisibleWork(r.id, r.title, r.kind) for r in rows]
