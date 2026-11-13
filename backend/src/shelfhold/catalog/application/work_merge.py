"""Merging two Works: a plan, a confirmation, a journal that can be undone for 30 days
(EF-BIB-019, EF-BIB-037, EF-BIB-049).

`plan_work_merge` reads and writes nothing. It says what moves from the Work that goes (the
absorbed one) into the Work that stays (the survivor) and what stops the merge:

- **structure**: a Work of another kind; two Editions that would share a key (that is an Edition
  merge, with its own plan: EF-BIB-049); the two Works in two different Series;
- nothing is merged silently: `apply_work_merge` acts for the fingerprint of a plan the admin saw,
  recomputed in the transaction that writes, so a plan that moved is stale.

The **age** never goes down: the survivor takes the strictest effective age of the two (an Edition
that has no age of its own inherits its Work's, so the age of the absorbed Work is carried to the
survivor before the Editions move), the adult flag is the union, and the survivor counts as unrated
as soon as one of the two was (the barrier of EF-BIB-055). The Editions are then put through the
filing gate again; a shelf below 18+ that no longer admits one is reported, never emptied (that is
`shelf.withdraw`, a reserved action).

Everything that moves is written into the snapshot of `merge_log`, which is what `separate_works`
reads to put the two Works back. The old identifier leads to the survivor (`work_redirect`), the
redirects that led to the absorbed Work are repointed, and every `?since=` token is cut off (a Work
was deleted: L11). The external identifiers of the absorbed Work cannot move (the table is
append-only): they stay and name a Work that is only reachable through the redirect.

The caller holds the write lock (`BEGIN IMMEDIATE`) and the transaction.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, exc

from shelfhold.catalog.application.changes import record_work_removed, track_work_changes
from shelfhold.catalog.application.merge import EDITION_KEY, StaleMergePlanError
from shelfhold.catalog.application.new_edition_gate import file_new_edition
from shelfhold.catalog.application.rating import RatingService
from shelfhold.infrastructure.event_sequence import invalidate_tokens
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, identity
from shelfhold.infrastructure.schema._base import metadata as _METADATA
from shelfhold.sdk.age import AgeRating, max_rating, to_columns

__all__ = [
    "MERGE_WINDOW_DAYS",
    "WorkMergePlan",
    "WorkMergeRefusedError",
    "WorkMerged",
    "apply_work_merge",
    "plan_work_merge",
    "resolve_work",
]

MERGE_WINDOW_DAYS = 30
_ENTITY_TABLES = ("contribution", "metadata_field", "age_suggestion")
_HOPS = 16
_RATING = RatingService()


class WorkMergeRefusedError(Exception):
    """The plan has blockers: nothing was changed. `plan` says which."""

    def __init__(self, plan: "WorkMergePlan") -> None:
        super().__init__("work merge refused: " + ", ".join(plan.blockers))
        self.plan = plan


@dataclass(frozen=True)
class WorkMergePlan:
    keep_id: str
    drop_id: str
    editions: tuple[str, ...]  # moved from the absorbed Work to the survivor
    blockers: tuple[str, ...]
    keep_age: str | None
    drop_age: str | None
    merged_age: str | None  # the strictest of the two: never lower than either
    unrated_after: bool  # one of the two was unrated: the survivor is, until an admin rates it
    fingerprint: str

    @property
    def mergeable(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class WorkMerged:
    merge_id: str
    plan: WorkMergePlan
    effective_age: AgeRating | None  # the survivor's now
    shelves: dict[str, str | None]  # every Edition of the survivor: the shelf it is filed on
    waiting: dict[str, str]  # the ones the shelf no longer admits (reason): never moved here


def _age(age_rating: int | None, age_x: bool) -> AgeRating | None:
    if age_x:
        return AgeRating(18, x=True)
    return None if age_rating is None else AgeRating(int(age_rating))


def _label(age: AgeRating | None) -> str | None:
    if age is None:
        return None
    return "X" if age.x else f"{age.min_age}+"


def _row(connection: Connection, work_id: str) -> sa.Row[Any]:
    row = connection.execute(sa.select(catalog.work).where(catalog.work.c.id == work_id)).first()
    if row is None:
        raise LookupError(f"no such Work: {work_id}")
    return row


def resolve_work(connection: Connection, work_id: str) -> str:
    """Where an old identifier leads: the Work that absorbed it, through any chain of merges. A
    Work that exists is its own answer; an unknown identifier is answered as it came (the
    caller's filter treats it as absent). A loop in the table ends where it was detected."""
    seen = {work_id}
    current = work_id
    for _ in range(_HOPS):
        target = connection.execute(
            sa.select(catalog.work_redirect.c.to_work_id).where(
                catalog.work_redirect.c.from_work_id == current
            )
        ).scalar()
        if target is None or target in seen:
            return current
        seen.add(str(target))
        current = str(target)
    return current


def _editions_of(connection: Connection, work_id: str) -> list[sa.Row[Any]]:
    return list(
        connection.execute(sa.select(catalog.edition).where(catalog.edition.c.work_id == work_id))
    )


def _series_of(connection: Connection, work_id: str) -> list[sa.Row[Any]]:
    return list(
        connection.execute(
            sa.select(catalog.series_member).where(catalog.series_member.c.work_id == work_id)
        )
    )


def _blockers(connection: Connection, keep: sa.Row[Any], drop: sa.Row[Any]) -> list[str]:
    out: list[str] = []
    if keep.kind != drop.kind:
        out.append("different_kind")
    keys = {tuple(e._mapping[c] for c in EDITION_KEY if c != "work_id") for e in _editions_of(
        connection, keep.id)}  # fmt: skip
    for e in _editions_of(connection, drop.id):
        if tuple(e._mapping[c] for c in EDITION_KEY if c != "work_id") in keys:
            out.append("collision:edition_key")
            break
    mine, theirs = _series_of(connection, keep.id), _series_of(connection, drop.id)
    if mine and theirs and {r.series_id for r in mine} != {r.series_id for r in theirs}:
        out.append("different_series")
    return out


def _effective(connection: Connection, work_id: str) -> AgeRating | None:
    return _RATING.effective_age_of(connection, work_id)


def plan_work_merge(connection: Connection, keep_id: str, drop_id: str) -> WorkMergePlan:
    """What merging `drop_id` into `keep_id` would do. `LookupError` for a Work that does not
    exist, `ValueError` for a Work merged into itself. Writes nothing."""
    if keep_id == drop_id:
        raise ValueError("a Work is not merged into itself")
    keep, drop = _row(connection, keep_id), _row(connection, drop_id)
    blockers = sorted(set(_blockers(connection, keep, drop)))
    keep_age, drop_age = _effective(connection, keep_id), _effective(connection, drop_id)
    merged = max_rating(a for a in (keep_age, drop_age) if a is not None)
    unrated = keep.rated_at is None or drop.rated_at is None
    editions = tuple(sorted(e.id for e in _editions_of(connection, drop_id)))
    body: dict[str, Any] = {
        "keep": keep_id, "drop": drop_id, "editions": editions, "blockers": blockers,
        "keep_age": _label(keep_age), "drop_age": _label(drop_age), "unrated": unrated,
        "keep_editions": sorted(e.id for e in _editions_of(connection, keep_id)),
        "keep_rated": str(keep.rated_at), "drop_rated": str(drop.rated_at),
        "series": sorted((r.series_id, r.position) for r in _series_of(connection, drop_id)),
    }  # fmt: skip
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return WorkMergePlan(
        keep_id, drop_id, editions, tuple(blockers), _label(keep_age), _label(drop_age),
        _label(merged), unrated, digest,
    )  # fmt: skip


def _jsonable(row: sa.Row[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row._mapping.items():
        out[str(key)] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _table(name: str) -> sa.Table:
    return _METADATA.tables[name]


def move_editions(connection: Connection, edition_ids: list[str], work_id: str) -> None:
    """The Editions and what is tied to their Work by a composite key (bindings, follows),
    together: the foreign keys are checked at the end of the transaction."""
    connection.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
    for edition_id in edition_ids:
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == edition_id)
            .values(work_id=work_id)
        )
        for table in (acquisition.source_binding, acquisition.series_follow):
            connection.execute(
                sa.update(table).where(table.c.edition_id == edition_id).values(work_id=work_id)
            )


_PROTECTION = {"rejected": 0, "accepted": 1, "open": 2}  # the more protective, the higher


def _settle_twin(
    connection: Connection, table: sa.Table, data: dict[str, Any], keep_id: str
) -> None:
    """A suggested age is never lost in a merge. Two suggestions of the same key (the key does not
    carry the state) become one row, the survivor's, in the **most protective** state of the two
    (open before accepted before rejected), and the snapshot notes what was folded into it. An
    undo never puts it back in a less protective state than it has then."""
    twin = connection.execute(
        sa.select(table.c.id, table.c.state).where(
            table.c.entity_type == "work",
            table.c.entity_id == keep_id,
            table.c.value == data["value"],
            table.c.x == data["x"],
            table.c.provenance == data["provenance"],
        )
    ).first()
    if twin is None:
        return
    if _PROTECTION[data["state"]] > _PROTECTION[twin.state]:
        connection.execute(
            sa.update(table).where(table.c.id == twin.id).values(state=data["state"])
        )
    data["_folded_into"] = {"id": twin.id, "state_before": twin.state}


def _move_entity_rows(connection: Connection, drop_id: str, keep_id: str) -> dict[str, Any]:
    """Credits, fields and suggested ages of the absorbed Work go to the survivor; a row the
    survivor already holds is dropped, and both kinds are in the snapshot."""
    saved: dict[str, Any] = {}
    for name in _ENTITY_TABLES:
        table = _table(name)
        rows = connection.execute(
            sa.select(table).where(table.c.entity_type == "work", table.c.entity_id == drop_id)
        ).all()
        kept: list[dict[str, Any]] = []
        for row in rows:
            data = _jsonable(row)
            pk = {c.name: row._mapping[c.name] for c in table.primary_key.columns}
            try:
                with connection.begin_nested():
                    connection.execute(
                        sa.update(table)
                        .where(*(table.c[k] == v for k, v in pk.items()))
                        .values(entity_id=keep_id)
                    )
                data["_moved"] = True
            except exc.IntegrityError:
                if name == "age_suggestion":
                    _settle_twin(connection, table, data, keep_id)
                connection.execute(
                    sa.delete(table).where(*(table.c[k] == v for k, v in pk.items()))
                )
                data["_moved"] = False
            kept.append(data)
        saved[name] = kept
    return saved


def _move_states(connection: Connection, drop_id: str, keep_id: str) -> list[dict[str, Any]]:
    """The flags of each user. Where the user holds both, the survivor's row takes the union and
    the latest stamp; the absorbed row is in the snapshot either way."""
    state = identity.user_state
    out: list[dict[str, Any]] = []
    for row in connection.execute(sa.select(state).where(state.c.work_id == drop_id)).all():
        data = _jsonable(row)
        mine = connection.execute(
            sa.select(state).where(state.c.user_id == row.user_id, state.c.work_id == keep_id)
        ).first()
        if mine is None:
            connection.execute(
                sa.update(state)
                .where(state.c.user_id == row.user_id, state.c.work_id == drop_id)
                .values(work_id=keep_id)
            )
            data["_moved"] = True
        else:
            read = max((t for t in (mine.read_at, row.read_at) if t is not None), default=None)
            connection.execute(
                sa.update(state)
                .where(state.c.user_id == row.user_id, state.c.work_id == keep_id)
                .values(
                    followed=bool(mine.followed or row.followed),
                    favorite=bool(mine.favorite or row.favorite),
                    read_at=read, stamp=max(mine.stamp, row.stamp),
                )
            )  # fmt: skip
            connection.execute(
                sa.delete(state).where(state.c.user_id == row.user_id, state.c.work_id == drop_id)
            )
            data["_moved"] = False
        out.append(data)
    return out


def apply_work_merge(
    connection: Connection, keep_id: str, drop_id: str, *, confirm: str, now: datetime
) -> WorkMerged:
    """Merge for the fingerprint of the plan the admin read, recomputed here.

    `StaleMergePlanError` when the plan is not the one of the data now (including a Work that a
    racing merge already took); `WorkMergeRefusedError` when it has blockers. In both cases
    nothing was written."""
    try:
        plan = plan_work_merge(connection, keep_id, drop_id)
    except LookupError:
        raise StaleMergePlanError(drop_id) from None
    if not hmac.compare_digest(plan.fingerprint.encode(), confirm.strip().encode()):
        raise StaleMergePlanError(plan.fingerprint)
    if not plan.mergeable:
        raise WorkMergeRefusedError(plan)
    with track_work_changes(connection, [keep_id], now=now):
        return _apply(connection, plan, now)


def _apply(connection: Connection, plan: WorkMergePlan, now: datetime) -> WorkMerged:
    keep_id, drop_id = plan.keep_id, plan.drop_id
    keep, drop = _row(connection, keep_id), _row(connection, drop_id)
    # the strictest age of the two **Works**: an Edition with an age of its own keeps it, and one
    # without inherits the Work's, so the absorbed Work's age protects its Editions (EF-BIB-019)
    merged = max_rating(
        a for a in (_age(keep.age_rating, keep.age_x), _age(drop.age_rating, drop.age_x)) if a
    )
    titles = [_jsonable(r) for r in connection.execute(
        sa.select(catalog.work_title).where(catalog.work_title.c.work_id == drop_id))]  # fmt: skip
    members = [_jsonable(r) for r in _series_of(connection, drop_id)]
    redirects = [
        str(r) for r in connection.execute(
            sa.select(catalog.work_redirect.c.from_work_id).where(
                catalog.work_redirect.c.to_work_id == drop_id
            )
        ).scalars()
    ]  # fmt: skip
    requests = [
        str(r) for r in connection.execute(
            sa.select(identity.request.c.id).where(identity.request.c.work_id == drop_id)
        ).scalars()
    ]  # fmt: skip
    merge_id = new_ulid()
    snapshot: dict[str, Any] = {
        "keep_row": _jsonable(keep), "drop_row": _jsonable(drop), "titles": titles,
        "editions": list(plan.editions), "members": members, "redirects": redirects,
        "requests": requests, "fingerprint": plan.fingerprint,
    }  # fmt: skip
    savepoint = connection.begin_nested()
    try:
        move_editions(connection, list(plan.editions), keep_id)
        snapshot["entities"] = _move_entity_rows(connection, drop_id, keep_id)
        snapshot["states"] = _move_states(connection, drop_id, keep_id)
        connection.execute(
            sa.update(identity.request)
            .where(identity.request.c.work_id == drop_id)
            .values(work_id=keep_id)
        )
        have = {r.series_id for r in _series_of(connection, keep_id)}
        for m in members:
            if m["series_id"] in have:  # the same Series: the survivor's place stays
                connection.execute(
                    sa.delete(catalog.series_member).where(
                        catalog.series_member.c.work_id == drop_id
                    )
                )
                m["_moved"] = False
            else:
                connection.execute(
                    sa.update(catalog.series_member)
                    .where(catalog.series_member.c.work_id == drop_id)
                    .values(work_id=keep_id)
                )
                m["_moved"] = True
        values: dict[str, Any] = {}
        if merged is not None:
            age, x = to_columns(merged)
            values.update(age_rating=age, age_x=x)
        values["is_adult"] = bool(keep.is_adult or drop.is_adult)
        values["rated_at"] = (
            None if (keep.rated_at is None or drop.rated_at is None) else keep.rated_at
        )
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == keep_id).values(**values)
        )
        snapshot["after"] = {
            "age_rating": values.get("age_rating", keep.age_rating),
            "age_x": values.get("age_x", keep.age_x), "is_adult": values["is_adult"],
            "rated_at": values["rated_at"].isoformat() if values["rated_at"] else None,
        }  # fmt: skip
        connection.execute(
            sa.update(catalog.work_redirect)
            .where(catalog.work_redirect.c.to_work_id == drop_id)
            .values(to_work_id=keep_id)
        )
        connection.execute(
            sa.insert(catalog.merge_log).values(
                id=merge_id, kind="merge", survivor_id=keep_id, absorbed_id=drop_id,
                snapshot=snapshot, created_at=now,
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(catalog.work_redirect).values(
                from_work_id=drop_id, to_work_id=keep_id, merge_log_id=merge_id, created_at=now
            )
        )
        record_work_removed(connection, drop_id, now=now)  # before the row goes
        connection.execute(sa.delete(catalog.work).where(catalog.work.c.id == drop_id))
        savepoint.commit()
    except exc.IntegrityError:
        savepoint.rollback()  # a collision the plan could not see: nothing moved
        raise WorkMergeRefusedError(plan) from None
    invalidate_tokens(connection)  # a Work was deleted: no token issued before answers (L11)
    shelves, waiting = refile(connection, keep_id)
    return WorkMerged(merge_id, plan, _effective(connection, keep_id), shelves, waiting)


def refile(connection: Connection, work_id: str) -> tuple[dict[str, str | None], dict[str, str]]:
    """Put the Editions of a Work through the filing gate again, where each one is filed. An
    Edition already on a shelf is never moved (the withdrawal of a family shelf is reserved):
    what the shelf no longer admits is returned as waiting, with the reason."""
    shelves: dict[str, str | None] = {}
    waiting: dict[str, str] = {}
    for edition in _editions_of(connection, work_id):
        filing = file_new_edition(connection, edition.id, edition.shelf_id, keep_filed=True)
        shelves[edition.id] = filing.shelf_id
        if filing.waiting and filing.reason:
            waiting[edition.id] = filing.reason
    return shelves, waiting


def utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
