"""« Séparer » / « Ce n'est pas la même œuvre »: put back the two Works of a merge, within 30 days
(EF-BIB-019, EF-BIB-037).

The merge wrote what it moved into the snapshot of its journal row (`work_merge`); this gives it
back: the absorbed Work (same identifier, same titles), its Editions (with their bindings and
follows), its place in a Series, the state of each user, the requests, the credits, fields and
suggested ages. The redirects go as they were: the old identifier of the absorbed Work is a Work
again, the identifiers that led to it lead to it again.

What it does not do: take back what happened after the merge. An Edition created under the survivor
since stays there; an Edition that was merged away since is not brought back; a survivor that was
itself absorbed by another merge blocks the separation (separate that one first).

The age of each half. Nothing is lowered by an undo, and nothing is guessed by comparing values:
the history says whether the merged Work **changed hands since** (an admin rating of it in the
audit, or a later merge into the same survivor that is still in force). If not, each half gets its
own age back from the snapshot. If so, each half takes the **strictest** of its snapshot age and
the merged Work's age now, and the adult flag is the union. A half that was **unrated** before the
merge is unrated again, whatever the admin decided for the merged Work (chosen, fail closed: an
admin rating of the whole is not a rating of a half nobody rated alone). The shelves are put
through the gate again and what no longer fits is reported, never moved (`shelf.withdraw`).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.application.changes import track_work_changes
from shelfhold.catalog.application.work_merge import (
    MERGE_WINDOW_DAYS,
    _age,
    _effective,
    _table,
    move_editions,
    refile,
    utc,
)
from shelfhold.infrastructure.event_sequence import invalidate_tokens
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, identity
from shelfhold.sdk.age import AgeRating, max_rating, to_columns

__all__ = [
    "MergeAlreadyUndoneError",
    "MergeExpiredError",
    "Separated",
    "SeparationBlockedError",
    "separate_works",
]


class MergeExpiredError(Exception):
    """The merge is older than 30 days: it stays."""

    code = "merge.expired"


class MergeAlreadyUndoneError(Exception):
    """This merge was already separated."""

    code = "merge.already_undone"


class SeparationBlockedError(Exception):
    """The survivor is not there to give anything back (another merge took it)."""

    code = "merge.survivor_gone"


@dataclass(frozen=True)
class Separated:
    merge_id: str
    survivor_id: str
    restored_id: str
    survivor_age: AgeRating | None
    restored_age: AgeRating | None
    shelves: dict[str, str | None]
    waiting: dict[str, str]


def _at(value: Any) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _is_date(column: sa.Column[Any]) -> bool:
    kind = column.type
    return isinstance(kind, sa.DateTime) or "DateTime" in type(kind).__name__


def _reinsert(connection: Connection, table: sa.Table, data: dict[str, Any]) -> None:
    values = {k: v for k, v in data.items() if not k.startswith("_") and k in table.c}
    for column in table.c:
        if values.get(column.name) is not None and _is_date(column):
            values[column.name] = _at(values[column.name])
    connection.execute(sa.insert(table).values(**values).prefix_with("OR IGNORE"))


def separate_works(connection: Connection, merge_id: str, *, now: datetime) -> Separated:
    """Undo the merge `merge_id`. `LookupError`: no such merge (or not a merge);
    `MergeAlreadyUndoneError`, `MergeExpiredError` (after 30 days), `SeparationBlockedError`.
    In every refusal nothing was written."""
    log = catalog.merge_log
    row = connection.execute(sa.select(log).where(log.c.id == merge_id)).first()
    if row is None or row.kind != "merge":
        raise LookupError(f"no such merge: {merge_id}")
    if row.undone_at is not None:
        raise MergeAlreadyUndoneError(merge_id)
    if utc(now) > utc(row.created_at) + timedelta(days=MERGE_WINDOW_DAYS):
        raise MergeExpiredError(merge_id)
    keep_id, drop_id = str(row.survivor_id), str(row.absorbed_id)
    if (
        connection.execute(sa.select(catalog.work.c.id).where(catalog.work.c.id == keep_id)).first()
        is None
    ):
        raise SeparationBlockedError(merge_id)
    snap: dict[str, Any] = row.snapshot
    changed = _changed_since(connection, merge_id, keep_id, row.created_at)
    with track_work_changes(connection, [keep_id], now=now):
        return _undo(connection, merge_id, keep_id, drop_id, snap, now, changed)


def _changed_since(
    connection: Connection, merge_id: str, keep_id: str, created_at: datetime
) -> bool:
    """Whether the merged Work's age may have moved since this merge, from the history: an admin
    rating of it (audit, `at` the merge instant or later: a tie counts, closed), or a later merge
    into the same survivor that is still in force (its Editions and age came after)."""
    audit, log = identity.audit, catalog.merge_log
    rated = connection.execute(
        sa.select(audit.c.id)
        .where(audit.c.action == "rate", audit.c.at >= created_at,
               sa.or_(audit.c.target == f"work:{keep_id}",
                      audit.c.target.like(f"work:{keep_id};%")))
        .limit(1)
    ).first()  # fmt: skip
    later = connection.execute(
        sa.select(log.c.id)
        .where(log.c.kind == "merge", log.c.survivor_id == keep_id, log.c.id != merge_id,
               log.c.undone_at.is_(None), log.c.created_at >= created_at)
        .limit(1)
    ).first()  # fmt: skip
    return rated is not None or later is not None


def _half(before: dict[str, Any], current: sa.Row[Any], changed: bool) -> dict[str, Any]:
    """The age columns a half gets back (see the module docstring)."""
    if not changed:
        return {
            "age_rating": before["age_rating"], "age_x": bool(before["age_x"]),
            "is_adult": bool(before["is_adult"]), "rated_at": _at(before["rated_at"]),
        }  # fmt: skip
    top = max_rating(
        a for a in (_age(before["age_rating"], before["age_x"]),
                    _age(current.age_rating, current.age_x)) if a is not None
    )  # fmt: skip
    age, x = to_columns(top) if top is not None else (None, False)
    return {
        "age_rating": age, "age_x": x,
        "is_adult": bool(before["is_adult"] or current.is_adult),
        "rated_at": None if before["rated_at"] is None else current.rated_at,
    }  # fmt: skip


def _undo(
    connection: Connection,
    merge_id: str,
    keep_id: str,
    drop_id: str,
    snap: dict[str, Any],
    now: datetime,
    changed: bool,
) -> Separated:
    work = catalog.work
    merged_now = connection.execute(sa.select(work).where(work.c.id == keep_id)).one()
    drop = {k: v for k, v in snap["drop_row"].items() if k != "created_seq"}
    drop["created_at"] = _at(drop.get("created_at"))
    drop["updated_at"] = _at(drop.get("updated_at"))
    drop.update(_half(snap["drop_row"], merged_now, changed))
    connection.execute(sa.insert(work).values(**{k: v for k, v in drop.items() if k in work.c}))
    connection.execute(  # the Work was created before every token that exists: keep its sequence
        sa.update(work)
        .where(work.c.id == drop_id)
        .values(created_seq=snap["drop_row"]["created_seq"])
    )
    for title in snap["titles"]:
        _reinsert(connection, catalog.work_title, {k: v for k, v in title.items() if k != "id"})
    editions = [
        str(e) for e in connection.execute(
            sa.select(catalog.edition.c.id).where(
                catalog.edition.c.id.in_(snap["editions"]), catalog.edition.c.work_id == keep_id
            )
        ).scalars()
    ]  # fmt: skip
    move_editions(connection, editions, drop_id)
    for m in snap["members"]:
        member = catalog.series_member
        if m["_moved"]:
            connection.execute(
                sa.update(member)
                .where(member.c.series_id == m["series_id"], member.c.work_id == keep_id)
                .values(work_id=drop_id)
            )
        else:
            _reinsert(connection, member, m)
    state = identity.user_state
    for s in snap["states"]:
        if s["_moved"]:
            connection.execute(
                sa.update(state)
                .where(state.c.user_id == s["user_id"], state.c.work_id == keep_id)
                .values(work_id=drop_id)
            )
        else:
            row = {k: v for k, v in s.items() if not k.startswith("_")}
            row.update(stamp=_at(s["stamp"]), read_at=_at(s["read_at"]))
            connection.execute(sa.insert(state).values(**row).prefix_with("OR IGNORE"))
    for rid in snap["requests"]:
        connection.execute(
            sa.update(identity.request)
            .where(identity.request.c.id == rid, identity.request.c.work_id == keep_id)
            .values(work_id=drop_id)
        )
    for name, rows in snap["entities"].items():
        table = _table(name)
        for data in rows:
            pk = {c.name: data[c.name] for c in table.primary_key.columns}
            if data["_moved"]:
                connection.execute(
                    sa.update(table)
                    .where(*(table.c[k] == v for k, v in pk.items() if k != "entity_id"),
                           table.c.entity_type == "work", table.c.entity_id == keep_id)
                    .values(entity_id=drop_id)
                )  # fmt: skip
            else:
                _reinsert(connection, table, data)
                twin = data.get("_reopened")
                if twin:  # the survivor's settled suggestion that the merge had opened again
                    connection.execute(
                        sa.update(table)
                        .where(table.c.id == twin["id"], table.c.state == "open")
                        .values(state=twin["state"])
                    )
    redirect = catalog.work_redirect
    connection.execute(sa.delete(redirect).where(redirect.c.from_work_id == drop_id))
    for old in snap["redirects"]:
        connection.execute(
            sa.update(redirect).where(redirect.c.from_work_id == old).values(to_work_id=drop_id)
        )
    connection.execute(
        sa.update(work)
        .where(work.c.id == keep_id)
        .values(**_half(snap["keep_row"], merged_now, changed))
    )
    log = catalog.merge_log
    connection.execute(sa.update(log).where(log.c.id == merge_id).values(undone_at=now))
    connection.execute(
        sa.insert(log).values(
            id=new_ulid(), kind="split", survivor_id=keep_id, absorbed_id=drop_id,
            snapshot={"undoes": merge_id}, created_at=now,
        )
    )  # fmt: skip
    invalidate_tokens(connection)
    shelves, waiting = refile(connection, keep_id)
    more, more_waiting = refile(connection, drop_id)
    return Separated(
        merge_id, keep_id, drop_id, _effective(connection, keep_id),
        _effective(connection, drop_id), {**shelves, **more}, {**waiting, **more_waiting},
    )  # fmt: skip
