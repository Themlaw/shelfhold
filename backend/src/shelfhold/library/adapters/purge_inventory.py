"""What the registry of copies and the contents say a purge must reach (EF-BIB-052).

The library owns the storage tables, so the purge (an export-side orchestration, because it also
touches the manifest, the mirror and the tickets) reads them only through here and through
`library.replica` (the one door to the registry of copies). A copy is one `replica` row (a file
under a physical root) or one `trash_item` (an entry of a root's bin). Nothing here touches a
disk: it names paths, the purge deletes them.
"""

import json
import re
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.public import remove_from_search_index
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.schema import (
    acquisition,
    catalog,
    export,
    metadata,
    platform,
    storage,
)
from shelfhold.library.adapters.meta_projection import meta_rel
from shelfhold.library.adapters.origin_comicinfo import origin_rel
from shelfhold.library.adapters.origin_opf import origin_opf_rel
from shelfhold.library.domain.content_hash import CONTENT_HASH_BY, FILE_HASH_BY
from shelfhold.library.domain.copies import Copy, Hash
from shelfhold.library.domain.download_ticket import DownloadTicket
from shelfhold.library.replica import erase_replicas, replica_copies, replica_hashes

_content, _trash, _root = storage.content, storage.trash_item, platform.physical_root

__all__ = [
    "Copy",
    "Hash",
    "closed_request_ids",
    "content_hashes",
    "content_ids",
    "copies",
    "created_since",
    "derived_rels",
    "download_tickets",
    "erase_content_rows",
    "erase_history",
    "erase_work_rows",
    "leftover_tickets",
    "let_go_tickets",
    "request_ids",
    "title_needles",
    "unresolved_bin_items",
    "upstream_tokens",
]


def copies(
    connection: Connection, unit_ids: Collection[str], export_item_ids: Collection[str]
) -> list[Copy]:
    """Every file copy of these Units and export items, and every bin entry that holds one."""
    found = replica_copies(connection, unit_ids, export_item_ids)
    bin_owner = sa.or_(
        sa.and_(_trash.c.kind == "unit", _trash.c.ref.in_(unit_ids)),
        sa.and_(_trash.c.kind == "export_file", _trash.c.ref.in_(export_item_ids)),
    )
    for row in connection.execute(
        sa.select(_trash, _root.c.path_entry, _root.c.mode)
        .select_from(_trash.join(_root, _root.c.root_id == _trash.c.physical_root_id))
        .where(bin_owner)
        .order_by(_trash.c.id)
    ):
        entry = row.path.split("/", 1)[0]
        unit = row.ref if row.kind == "unit" else None
        item = row.ref if row.kind == "export_file" else None
        found.append(
            Copy("bin_entry", row.id, "trash", row.physical_root_id, row.path_entry, row.mode,
                 entry, unit, item)
        )  # fmt: skip
    return found


def unresolved_bin_items(connection: Connection, unit_ids: Collection[str]) -> int:
    """Bin items of these Units that sit in no root: they cannot be located, so they are
    reported and never guessed."""
    return int(
        connection.execute(
            sa.select(sa.func.count())
            .select_from(_trash)
            .where(
                _trash.c.kind == "unit",
                _trash.c.ref.in_(unit_ids),
                _trash.c.physical_root_id.is_(None),
            )
        ).scalar_one()
    )


def content_hashes(
    connection: Connection, unit_ids: Collection[str]
) -> tuple[list[Hash], list[Hash]]:
    """(payload hashes, derived hashes) of these Units, each with the algorithm that made it: what
    a `variant` tombstone cites, so the same file is refused wherever it comes back from."""
    payload: dict[tuple[str, str], Hash] = {}
    derived: dict[tuple[str, str], Hash] = {}
    file_rows = []
    for row in connection.execute(
        sa.select(
            _content.c.original_sha256, _content.c.content_hash, _content.c.content_hash_by,
            _content.c.derived_hash, _content.c.derived_hash_by, _content.c.derived_by,
        ).where(_content.c.unit_id.in_(unit_ids))
    ):  # fmt: skip
        file_rows.append(row)
        if row.content_hash:
            by = row.content_hash_by or CONTENT_HASH_BY
            payload[(by, row.content_hash)] = Hash(by, row.content_hash)
        if row.derived_hash:
            by = row.derived_hash_by or row.derived_by or CONTENT_HASH_BY
            derived[(by, row.derived_hash)] = Hash(by, row.derived_hash)
    for value in {row.original_sha256 for row in file_rows}:
        payload.setdefault((FILE_HASH_BY, value), Hash(FILE_HASH_BY, value))
    by_content, by_file = replica_hashes(connection, unit_ids)
    for value in by_content:
        payload.setdefault((CONTENT_HASH_BY, value), Hash(CONTENT_HASH_BY, value))
    for value in by_file:  # the rendered file of a destination: what an adoption would see
        payload.setdefault((FILE_HASH_BY, value), Hash(FILE_HASH_BY, value))
    return list(payload.values()), list(derived.values())


def erase_content_rows(
    connection: Connection, unit_ids: Collection[str], export_item_ids: Collection[str]
) -> None:
    """The storage rows of these Units: contents, copies, bin items. Run once the files are gone
    (nothing cascades from `content`, so a file is never forgotten on disk)."""
    connection.execute(sa.delete(_content).where(_content.c.unit_id.in_(unit_ids)))
    erase_replicas(connection, unit_ids, export_item_ids)
    connection.execute(
        sa.delete(_trash).where(
            sa.or_(
                sa.and_(_trash.c.kind == "unit", _trash.c.ref.in_(unit_ids)),
                sa.and_(_trash.c.kind == "export_file", _trash.c.ref.in_(export_item_ids)),
            )
        )
    )


def content_ids(connection: Connection, unit_ids: Collection[str]) -> list[str]:
    return list(
        connection.execute(
            sa.select(_content.c.id).where(_content.c.unit_id.in_(unit_ids))
        ).scalars()
    )


DERIVED_CACHES = ("cache/thumbnails", "cache/content")  # under /data, keyed by Unit


def derived_rels(unit_id: str) -> list[str]:
    """Paths under /data that hold something derived from a Unit: its `unit.json`, the ComicInfo
    the service sent, its thumbnails
    and its `/content` cache (`cache/<kind>/<id[:2]>/<id>`, like `meta/units`). A purge erases
    them; the writers of those caches follow the same layout."""
    if not is_ulid(unit_id):
        raise ValueError(f"not an identifier: {unit_id!r}")
    return [
        meta_rel(unit_id),
        origin_rel(unit_id),  # the ComicInfo as the service sent it (EF-BIB-048)
        origin_opf_rel(unit_id),  # the OPF before the exporter's edit (EF-BIB-048)
        *(f"{base}/{unit_id[:2]}/{unit_id}" for base in DERIVED_CACHES),
    ]


# -- what a purge asks of the service, and the rows only a Work purge reaches (L17) ----------------

_OPEN = ("requested", "downloaded", "fetched")
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_MIN_NEEDLE = 3  # a title shorter than this would match anything


def download_tickets(connection: Connection, unit_ids: Collection[str]) -> list[DownloadTicket]:
    """The requests of these Units that the service may still hold a chapter for: a ticket, and
    a state that says it is queued, downloaded or fetched (EF-SRC-033)."""
    r, v, b, si = (
        acquisition.download_request, catalog.unit_variant, acquisition.source_binding,
        acquisition.source_item,
    )  # fmt: skip
    rows = connection.execute(
        sa.select(r.c.id, r.c.ticket, v.c.unit_id, r.c.state, si.c.plugin_id, si.c.source_key)
        .select_from(
            r.join(v, v.c.id == r.c.variant_id)
            .join(b, b.c.id == r.c.source_binding_id)
            .join(si, si.c.id == b.c.source_item_id)
        )
        .where(
            v.c.unit_id.in_(unit_ids), r.c.target_kind == "unit", r.c.ticket.is_not(None),
            r.c.state.in_(_OPEN),
        )
        .order_by(r.c.requested_at, r.c.id)
    ).all()  # fmt: skip
    return [DownloadTicket(*tuple(row)) for row in rows]


def leftover_tickets(connection: Connection) -> list[DownloadTicket]:
    """What the purges of M1a left in the service: a request closed by a purge that still holds
    its ticket (M1a asked nothing of the service). The Unit is gone, so `unit_id` is empty. Read
    only; ordered, so that the version of a plan is stable."""
    r, b, si = acquisition.download_request, acquisition.source_binding, acquisition.source_item
    rows = connection.execute(
        sa.select(r.c.id, r.c.ticket, r.c.state, si.c.plugin_id, si.c.source_key)
        .select_from(
            r.join(b, b.c.id == r.c.source_binding_id).join(si, si.c.id == b.c.source_item_id)
        )
        .where(r.c.state == "closed_by_purge", r.c.ticket.is_not(None))
        .order_by(r.c.requested_at, r.c.id)
    ).all()  # fmt: skip
    return [
        DownloadTicket(row.id, row.ticket, "", row.state, row.plugin_id, row.source_key)
        for row in rows
    ]


def closed_request_ids(connection: Connection) -> list[str]:
    """Every request a purge closed, with a ticket or not: what a plan of leftovers is about."""
    r = acquisition.download_request
    return list(
        connection.execute(
            sa.select(r.c.id).where(r.c.state == "closed_by_purge").order_by(r.c.id)
        ).scalars()
    )


def let_go_tickets(connection: Connection, request_ids: Collection[str]) -> int:
    """The ticket of a closed request that the service no longer knows is forgotten."""
    r = acquisition.download_request
    return connection.execute(
        sa.update(r)
        .where(r.c.id.in_(request_ids), r.c.state == "closed_by_purge")
        .values(ticket=None, updated_at=sa.func.current_timestamp())
    ).rowcount


def request_ids(connection: Connection, unit_ids: Collection[str]) -> list[str]:
    """Every download request of these Units, whatever its state (jobs and events cite them)."""
    r, v = acquisition.download_request, catalog.unit_variant
    return list(
        connection.execute(
            sa.select(r.c.id)
            .select_from(r.join(v, v.c.id == r.c.variant_id))
            .where(v.c.unit_id.in_(unit_ids))
        ).scalars()
    )


def created_since(
    connection: Connection, unit_ids: Collection[str], work_id: str | None
) -> datetime | None:
    """The first instant the rows of the purge existed: a backup taken before holds none of them."""
    found = [
        connection.execute(
            sa.select(sa.func.min(catalog.unit.c.created_at)).where(catalog.unit.c.id.in_(unit_ids))
        ).scalar()
    ]
    if work_id is not None:
        found.append(
            connection.execute(
                sa.select(catalog.work.c.created_at).where(catalog.work.c.id == work_id)
            ).scalar()
        )
        # the series' rows (its source item, its follow request) may be older than any Unit: a
        # backup taken since the follow began holds them (the slug spells the title)
        si, fr, b = acquisition.source_item, acquisition.follow_request, acquisition.source_binding
        found.append(
            connection.execute(
                sa.select(sa.func.min(si.c.created_at))
                .select_from(si.join(b, b.c.source_item_id == si.c.id))
                .where(b.c.work_id == work_id)
            ).scalar()
        )
        found.append(
            connection.execute(
                sa.select(sa.func.min(fr.c.created_at))
                .select_from(
                    fr.join(
                        si, sa.and_(fr.c.source_key == si.c.source_key,
                                    fr.c.remote_id == si.c.remote_id),
                    ).join(b, b.c.source_item_id == si.c.id)
                )
                .where(b.c.work_id == work_id)
            ).scalar()
        )  # fmt: skip
    dates = [d for d in found if d is not None]
    return min(dates) if dates else None


def title_needles(
    connection: Connection, unit_ids: Collection[str], work_id: str | None
) -> list[str]:
    """What a reader would recognise: the titles of the Units, and for a Work its titles, its
    other names and the series it sits in. Lower case, never shorter than three letters."""
    found: set[str] = set()
    found.update(
        connection.execute(
            sa.select(catalog.unit.c.title).where(
                catalog.unit.c.id.in_(unit_ids), catalog.unit.c.title.is_not(None)
            )
        ).scalars()
    )
    if work_id is not None:
        w, t = catalog.work, catalog.work_title
        found.update(
            connection.execute(sa.select(w.c.title, w.c.sort_title).where(w.c.id == work_id)).one()
        )
        found.update(
            connection.execute(sa.select(t.c.title).where(t.c.work_id == work_id)).scalars()
        )
        found.update(
            connection.execute(
                sa.select(catalog.series.c.title, catalog.series.c.sort_title)
                .select_from(
                    catalog.series.join(
                        catalog.series_member,
                        catalog.series_member.c.series_id == catalog.series.c.id,
                    )
                )
                .where(catalog.series_member.c.work_id == work_id)
            ).scalars()
        )
    return sorted({n.strip().lower() for n in found if n and len(n.strip()) >= _MIN_NEEDLE})


def upstream_tokens(
    connection: Connection, unit_ids: Collection[str], work_id: str | None
) -> list[str]:
    """The upstream identifiers of the purged rows (the chapter's `remote_unit_id`, and for a Work
    the series' `remote_id`): a job or an error text quotes them, a URL slug spells the title."""
    v, si = catalog.unit_variant, acquisition.source_item
    found = set(
        connection.execute(sa.select(v.c.remote_unit_id).where(v.c.unit_id.in_(unit_ids))).scalars()
    )
    if work_id is not None:
        b = acquisition.source_binding
        found.update(
            connection.execute(
                sa.select(si.c.remote_id)
                .select_from(si.join(b, b.c.source_item_id == si.c.id))
                .where(b.c.work_id == work_id)
            ).scalars()
        )
    return sorted(t for t in found if t and len(t) >= _MIN_NEEDLE)


def _matcher(needles: Sequence[str]) -> re.Pattern[str] | None:
    """A title is found as a word (or a phrase), never inside another one: a Unit called "Port"
    must not take the jobs and notifications of "export" and "report" with it."""
    if not needles:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in needles) + r")(?!\w)")


def _cites(
    text: str,
    ids: Collection[str],
    titles: re.Pattern[str] | None,
    tokens: Sequence[str],
) -> bool:
    return (
        any(token in ids for token in _ULID.findall(text))
        or (titles is not None and titles.search(text.lower()) is not None)
        or any(token in text for token in tokens)
    )


FINISHED_JOBS: Final = ("succeeded", "failed", "cancelled", "dead_letter")


def erase_history(
    connection: Connection,
    ids: Collection[str],
    needles: Sequence[str],
    tokens: Sequence[str] = (),
) -> tuple[int, int]:
    """The jobs and the events (and the notifications waiting on them) that cite the purged rows:
    by an identifier of theirs, by an upstream identifier (`tokens`: a chapter or series key), or
    by a title as a whole word in their text (an error message, a payload). Only a job that is
    over goes: a queued or running one is somebody's work, and its own run settles it. A job or an
    event that cites nothing is not touched. Returns `(jobs, events)` erased."""
    job, event, pending = platform.job_view, platform.event, platform.notification_pending
    wanted = set(ids)
    titles = _matcher(needles)
    jobs = [
        row.id
        for row in connection.execute(
            sa.select(
                job.c.id,
                job.c.request_id,
                job.c.dedupe_key,
                job.c.last_error,
                job.c.cursor,
                job.c.source_key,
            ).where(job.c.status.in_(FINISHED_JOBS))
        )
        if row.request_id in wanted
        or _cites(" ".join(str(c or "") for c in row[2:]), wanted, titles, tokens)
    ]
    for chunk in _chunks(jobs):
        connection.execute(sa.delete(job).where(job.c.id.in_(chunk)))
    events = [
        row.id
        for row in connection.execute(sa.select(event.c.id, event.c.payload))
        if _cites(json.dumps(row.payload, ensure_ascii=False), wanted, titles, tokens)
    ]
    for chunk in _chunks(events):
        connection.execute(sa.delete(pending).where(pending.c.event_id.in_(chunk)))
        connection.execute(sa.delete(event).where(event.c.id.in_(chunk)))
    return len(jobs), len(events)


def _chunks(values: Sequence[Any], size: int = 400) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _erase_with_dependents(
    connection: Connection, table: sa.Table, where: sa.ColumnElement[bool], path: tuple[str, ...]
) -> None:
    """Delete the rows of `table` that match `where`, after every row that names them by a foreign
    key, whatever the cascade says: nothing relies on a pragma to leave no trace. A reference that
    loops back (a translation of another Edition) is let go of, not followed."""
    for child in metadata.tables.values():
        for constraint in child.foreign_key_constraints:
            if constraint.referred_table is not table:
                continue
            columns = list(constraint.columns)
            targets = [element.column for element in constraint.elements]
            reached = sa.select(*targets).where(where)
            condition = (
                columns[0].in_(reached) if len(columns) == 1 else sa.tuple_(*columns).in_(reached)
            )
            if child is table or child.name in path:
                if all(c.nullable for c in columns):
                    connection.execute(
                        sa.update(child).where(condition).values({c.name: None for c in columns})
                    )
                continue
            _erase_with_dependents(connection, child, condition, (*path, table.name))
    connection.execute(sa.delete(table).where(where))


def erase_work_rows(connection: Connection, work_id: str) -> None:
    """The metadata of a purged Work (EF-BIB-053 (4)): the Work, its Editions and everything that
    hangs from them, the upstream series it was bound to (and what the service last said of
    them: titles), and a series left with no Work. The Units are gone before this runs."""
    b, si, snap = acquisition.source_binding, acquisition.source_item, acquisition.upstream_snapshot
    items = list(
        connection.execute(sa.select(b.c.source_item_id).where(b.c.work_id == work_id)).scalars()
    )
    pairs = [
        (row.source_key, row.remote_id)
        for row in connection.execute(
            sa.select(si.c.source_key, si.c.remote_id).where(si.c.id.in_(items))
        )
    ]
    keys = [f"{source_key}|{remote_id}" for source_key, remote_id in pairs]
    series = list(
        connection.execute(
            sa.select(catalog.series_member.c.series_id).where(
                catalog.series_member.c.work_id == work_id
            )
        ).scalars()
    )
    placed = export.edition_export_series
    folders = list(
        connection.execute(
            sa.select(placed.c.export_series_id)
            .select_from(placed.join(catalog.edition, catalog.edition.c.id == placed.c.edition_id))
            .where(catalog.edition.c.work_id == work_id)
        ).scalars()
    )  # the series folder a destination named after the Work: its name is the title
    _erase_with_dependents(connection, catalog.work, catalog.work.c.id == work_id, ())
    remove_from_search_index(connection, [work_id])  # EF-BIB-010: not found after the purge
    _erase_with_dependents(
        connection, export.export_series,
        sa.and_(
            export.export_series.c.id.in_(folders),
            ~sa.exists().where(placed.c.export_series_id == export.export_series.c.id),
        ),
        (),
    )  # fmt: skip
    unshared = sa.and_(si.c.id.in_(items), ~sa.exists().where(b.c.source_item_id == si.c.id))
    _erase_with_dependents(connection, si, unshared, ())
    connection.execute(sa.delete(snap).where(snap.c.series_ref.in_(keys)))
    # `follow_request` is keyed by the upstream series, with no foreign key: its row spells the
    # series (a URL slug) and would outlive the Work. A series another Work still binds stays.
    follow = acquisition.follow_request
    for source_key, remote_id in pairs:
        still_bound = connection.execute(
            sa.select(si.c.id).where(si.c.source_key == source_key, si.c.remote_id == remote_id)
        ).first()
        if still_bound is None:
            connection.execute(
                sa.delete(follow).where(
                    follow.c.source_key == source_key, follow.c.remote_id == remote_id
                )
            )
    member = catalog.series_member
    _erase_with_dependents(
        connection,
        catalog.series,
        sa.and_(
            catalog.series.c.id.in_(series),
            ~sa.exists().where(member.c.series_id == catalog.series.c.id),
        ),
        (),
    )
