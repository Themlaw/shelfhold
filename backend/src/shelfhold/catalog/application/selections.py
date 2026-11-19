"""Saved selections: stored queries, evaluated on the fly in the reader's scope (M5 L21, EF-BIB-017,
EF-INT-007).

A selection is a name and a query of the closed grammar of `catalog.domain.selection_query`. It
holds no Work and no result. What a reader is shown of one is computed when she asks, as one more
condition added to the one filter of the catalogue (`queries.visible_works_clause`, never instead of
it): a child gets no hidden Work, no hidden Edition for a shelf or a language, and no count that
includes one, whatever the query says. The same condition serves the library and the OPDS feed.

- A selection belongs to the profile that made it and is read by that profile alone: not by an
  admin, not by another profile, not by a key of another profile (no sharing in M5). A selection
  that is not hers is exactly one that does not exist (`SelectionNotFoundError`).
- The query is checked on the way in and again on the way out: a stored text that is no longer the
  grammar (a restore, a hand edit) selects nothing and says so (`valid`), it is never evaluated.
- The evaluation is bounded: a page is at most `queries.MAX_PAGE` Works, the count stops at
  `COUNT_CAP` (and says so), and a statement that runs longer than `EVAL_BUDGET_SECONDS` is
  interrupted (`SelectionTooSlowError`) and leaves the connection as it found it.
- A Work or a shelf that leaves the catalogue changes nothing in a selection: it is only a query.
"""

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa

from shelfhold.catalog.application.queries import (
    ReaderScope,
    VisibleWork,
    count_visible_works,
    list_visible_works,
    visible_editions_clause,
)
from shelfhold.catalog.domain.selection_query import (
    QueryRefusedError,
    SelectionQuery,
    dump_query,
    parse_query,
)
from shelfhold.infrastructure.db import read_transaction, write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog

__all__ = [
    "COUNT_CAP",
    "EVAL_BUDGET_SECONDS",
    "MAX_SELECTIONS",
    "SelectionFullError",
    "SelectionNotFoundError",
    "SelectionPage",
    "SelectionRecord",
    "SelectionTooSlowError",
    "SelectionViewer",
    "Selections",
]

MAX_SELECTIONS: int = 50  # per profile
COUNT_CAP: int = 10_000  # the count never reads further
EVAL_BUDGET_SECONDS: float = 2.0  # one evaluation (a page and its count) may not run longer
PROGRESS_STEP: int = 1000  # virtual machine steps between two looks at the clock
_STATES: Final = ("unread", "in_progress", "read")


class SelectionNotFoundError(LookupError):
    """Absent, or not the reader's: the same error, so nothing tells the two apart."""


class SelectionFullError(ValueError):
    pass


class SelectionTooSlowError(RuntimeError):
    """The evaluation ran out of time and was interrupted."""


@dataclass(frozen=True)
class SelectionViewer:
    """Who reads: the profile (the only owner) and her scope (her ceiling)."""

    user_id: str
    scope: ReaderScope


@dataclass(frozen=True)
class SelectionRecord:
    id: str
    name: str
    query: SelectionQuery | None  # None: the stored text is not the grammar (it selects nothing)
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SelectionPage:
    works: list[VisibleWork]
    total: int  # of the Works she may see; at most `COUNT_CAP`
    capped: bool
    valid: bool


_PROGRESS = sa.table(  # the view of M4 (`reading_progress`): where each Unit stands, per profile
    "reading_progress",
    sa.column("user_id", sa.String()),
    sa.column("unit_id", sa.String()),
    sa.column("state", sa.String()),
)


def _record(row: Any) -> SelectionRecord:
    try:
        query: SelectionQuery | None = parse_query(row.query_json)
    except QueryRefusedError:
        query = None
    return SelectionRecord(row.id, row.name, query, row.created_at, row.updated_at)


def _reading_state(user_id: str, states: tuple[str, ...]) -> sa.ColumnElement[bool]:
    """The state of a Work for this profile, from her own Units (`reading_progress`):
    `unread` (no Unit opened), `in_progress` (one opened, no Edition fully read), `read` (every
    Unit of one Edition read). A Work with no Unit has not been opened."""
    work = catalog.work.c
    mine = (
        sa.select(_PROGRESS.c.unit_id, _PROGRESS.c.state)
        .where(_PROGRESS.c.user_id == user_id)
        .cte("selection_progress")
    )
    unit_a, edition_a = catalog.unit.alias("sel_unit_a"), catalog.edition.alias("sel_edition_a")
    started = (
        sa.select(sa.literal(1))
        .select_from(unit_a.join(edition_a, edition_a.c.id == unit_a.c.edition_id))
        .where(
            edition_a.c.work_id == work.id,
            sa.exists().where(
                mine.c.unit_id == unit_a.c.id, mine.c.state.in_(("read", "in_progress"))
            ),
        )
        .exists()
    )
    edition_b = catalog.edition.alias("sel_edition_b")
    unit_b, unit_c = catalog.unit.alias("sel_unit_b"), catalog.unit.alias("sel_unit_c")
    fully_read = (
        sa.select(sa.literal(1))
        .select_from(edition_b)
        .where(
            edition_b.c.work_id == work.id,
            sa.exists().where(unit_b.c.edition_id == edition_b.c.id),
            ~sa.exists().where(
                unit_c.c.edition_id == edition_b.c.id,
                ~sa.exists().where(mine.c.unit_id == unit_c.c.id, mine.c.state == "read"),
            ),
        )
        .exists()
    )
    wanted: list[sa.ColumnElement[bool]] = []
    if "unread" in states:
        wanted.append(~started)
    if "in_progress" in states:
        wanted.append(sa.and_(started, ~fully_read))
    if "read" in states:
        wanted.append(fully_read)
    return sa.or_(*wanted)


def conditions(
    scope: ReaderScope, user_id: str, query: SelectionQuery, now: datetime
) -> list[sa.ColumnElement[bool]]:
    """The query as conditions on `work`, to be added to the one filter of the catalogue. Every
    value is a bound parameter; an Edition counts only if she may see it."""
    work, edition = catalog.work.c, catalog.edition.c
    found: list[sa.ColumnElement[bool]] = []
    if query.reading_state:
        found.append(_reading_state(user_id, query.reading_state))
    if query.kind:
        found.append(work.kind.in_(query.kind))
    if query.shelf:
        found.append(
            sa.select(sa.literal(1))
            .where(
                edition.work_id == work.id, edition.shelf_id.in_(query.shelf),
                visible_editions_clause(scope),
            )
            .exists()
        )  # fmt: skip
    if query.lang:
        lowered = sa.func.lower(edition.lang)
        spoken = sa.or_(
            *(sa.or_(lowered == tag, lowered.like(f"{tag}-%")) for tag in query.lang)
        )  # `fr` is also `fr-CA`; a tag holds neither `%` nor `_`
        found.append(
            sa.select(sa.literal(1))
            .where(edition.work_id == work.id, spoken, visible_editions_clause(scope))
            .exists()
        )
    if query.added_within_days is not None:
        found.append(work.created_at >= now - timedelta(days=query.added_within_days))
    return found


@contextmanager
def bounded(connection: sa.Connection) -> Iterator[None]:
    """The statements run inside stop when `EVAL_BUDGET_SECONDS` is over."""
    raw = connection.connection.dbapi_connection
    if not isinstance(raw, sqlite3.Connection):
        yield  # no driver to ask: the page and the count are bounded all the same
        return
    deadline = time.monotonic() + EVAL_BUDGET_SECONDS
    raw.set_progress_handler(lambda: int(time.monotonic() > deadline), PROGRESS_STEP)
    try:
        yield
    except sa.exc.OperationalError as failed:
        if "interrupt" in str(failed).lower():
            raise SelectionTooSlowError from None
        raise
    finally:
        raw.set_progress_handler(None, 0)


class Selections:
    def __init__(self, engine: sa.Engine, now: Callable[[], datetime]) -> None:
        self._engine = engine
        self._now = now

    # -- the profile's own selections ---------------------------------------------------------

    def list_for(self, viewer: SelectionViewer) -> list[SelectionRecord]:
        table = catalog.saved_selection.c
        with read_transaction(self._engine) as connection:
            rows = connection.execute(
                sa.select(
                    table.id, table.name, table.query_json, table.created_at, table.updated_at
                )
                .where(table.owner_user_id == viewer.user_id)
                .order_by(table.created_at, table.id)
                .limit(MAX_SELECTIONS)
            ).all()
        return [_record(row) for row in rows]

    def get(self, viewer: SelectionViewer, selection_id: str) -> SelectionRecord:
        with read_transaction(self._engine) as connection:
            return self._read(connection, viewer, selection_id)

    def _read(
        self, connection: sa.Connection, viewer: SelectionViewer, selection_id: str
    ) -> SelectionRecord:
        table = catalog.saved_selection.c
        row = connection.execute(
            sa.select(table.id, table.name, table.query_json, table.created_at, table.updated_at)
            .where(table.id == selection_id, table.owner_user_id == viewer.user_id)
        ).first()  # fmt: skip
        if row is None:
            raise SelectionNotFoundError(selection_id)
        return _record(row)

    def create(self, viewer: SelectionViewer, *, name: str, query: SelectionQuery) -> str:
        table = catalog.saved_selection
        selection_id = new_ulid()
        with write_transaction(self._engine) as connection:
            held = connection.execute(
                sa.select(sa.func.count()).where(table.c.owner_user_id == viewer.user_id)
            ).scalar_one()
            if held >= MAX_SELECTIONS:
                raise SelectionFullError
            connection.execute(
                sa.insert(table).values(
                    id=selection_id, owner_user_id=viewer.user_id, name=name,
                    query_json=dump_query(query),
                )
            )  # fmt: skip
        return selection_id

    def update(
        self, viewer: SelectionViewer, selection_id: str, *,
        name: str | None = None, query: SelectionQuery | None = None,
    ) -> SelectionRecord:  # fmt: skip
        table = catalog.saved_selection
        changes: dict[str, object] = {}
        if name is not None:
            changes["name"] = name
        if query is not None:
            changes["query_json"] = dump_query(query)
        with write_transaction(self._engine) as connection:
            self._read(connection, viewer, selection_id)
            if changes:
                connection.execute(
                    sa.update(table)
                    .where(table.c.id == selection_id, table.c.owner_user_id == viewer.user_id)
                    .values(**changes, updated_at=sa.func.current_timestamp())
                )
            return self._read(connection, viewer, selection_id)

    def delete(self, viewer: SelectionViewer, selection_id: str) -> None:
        table = catalog.saved_selection
        with write_transaction(self._engine) as connection:
            gone = connection.execute(
                sa.delete(table).where(
                    table.c.id == selection_id, table.c.owner_user_id == viewer.user_id
                )
            ).rowcount
        if not gone:
            raise SelectionNotFoundError(selection_id)

    # -- the evaluation -------------------------------------------------------------------------

    def evaluate(
        self,
        connection: sa.Connection,
        viewer: SelectionViewer,
        record: SelectionRecord,
        *,
        limit: int,
        offset: int,
    ) -> SelectionPage:
        """What `viewer` is shown of the selection now: the Works of the page and the count, both
        made of what her scope lets her see, within the time and the size the module bounds."""
        if record.query is None:
            return SelectionPage([], 0, False, False)
        extra = conditions(viewer.scope, viewer.user_id, record.query, self._now())
        with bounded(connection):
            works = list_visible_works(
                connection, viewer.scope, limit=limit, offset=offset, extra=extra
            )
            counted = count_visible_works(connection, viewer.scope, extra=extra, cap=COUNT_CAP + 1)
        return SelectionPage(works, min(counted, COUNT_CAP), counted > COUNT_CAP, True)

    def page(
        self, viewer: SelectionViewer, selection_id: str, *, limit: int, offset: int
    ) -> tuple[SelectionRecord, SelectionPage]:
        with read_transaction(self._engine) as connection:
            record = self._read(connection, viewer, selection_id)
            return record, self.evaluate(connection, viewer, record, limit=limit, offset=offset)
