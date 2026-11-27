"""What was made of one Edition for the destinations and the e-readers: the admin's read.

Nothing new is decided here, everything is read from what the export and the OPDS feed already
wrote:

* `items`: the exported objects of the Edition (`export_item`) by kind;
* `destinations`: for each destination that holds a record of one of them, how many stand for a
  file there (`LIVE_STATES`, the reconcile's definition), how many wait to be written and how many
  failed (`error`, `missing`), and when the last one was exported; a recycled record is history;
* `kepub_items`: the items a Kobo is offered as KEPUB (a live record and an EPUB content, the rule
  of `reading.application.kepub_delivery`);
* `blocks`: the blocks of the series made for an e-reader (`bundle_version`, not purged), with the
  profile they were made for.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

import sqlalchemy as sa

from shelfhold.infrastructure.export_states import LIVE_STATES
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.schema import bundle, catalog, export, identity, storage

__all__ = ["EditionBlock", "EditionDestination", "EditionExports", "edition_exports"]

EXPORT_ITEM_KINDS: Final = export.EXPORT_ITEM_KINDS
_PENDING: Final = frozenset({"planned"})
_FAILED: Final = frozenset({"error", "missing"})


@dataclass(frozen=True)
class EditionDestination:
    destination_id: str
    target_id: str
    target_name: str
    target_kind: str
    shelf_name: str
    enabled: bool
    exported: int
    pending: int
    failed: int
    last_exported_at: datetime | None


@dataclass(frozen=True)
class EditionBlock:
    profile_id: str
    username: str
    part_index: int
    number: int
    size: int
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True)
class EditionExports:
    edition_id: str
    items: dict[str, int]
    destinations: list[EditionDestination]
    kepub_items: int
    blocks: list[EditionBlock]


def edition_exports(connection: sa.Connection, edition_id: str) -> EditionExports | None:
    """The exports of the Edition, or None when it does not exist."""
    if not is_ulid(edition_id):
        return None
    found = connection.execute(
        sa.select(catalog.edition.c.id).where(catalog.edition.c.id == edition_id)
    ).first()
    if found is None:
        return None
    item = export.export_item
    items = dict.fromkeys(EXPORT_ITEM_KINDS, 0)
    for row in connection.execute(
        sa.select(item.c.kind, sa.func.count())
        .where(item.c.edition_id == edition_id)
        .group_by(item.c.kind)
    ):
        items[str(row[0])] = int(row[1])
    return EditionExports(
        edition_id=edition_id,
        items=items,
        destinations=_destinations(connection, edition_id),
        kepub_items=_kepub_items(connection, edition_id),
        blocks=_blocks(connection, edition_id),
    )


def _destinations(connection: sa.Connection, edition_id: str) -> list[EditionDestination]:
    record, item = export.export_record, export.export_item
    dest, target, shelf = export.shelf_destination, export.export_target, export.shelf

    def counted(states: frozenset[str]) -> sa.ColumnElement[int]:
        return sa.func.sum(sa.case((record.c.state.in_(sorted(states)), 1), else_=0))

    rows = connection.execute(
        sa.select(
            dest.c.id, dest.c.enabled, target.c.id.label("target_id"),
            target.c.name.label("target_name"), target.c.kind.label("target_kind"),
            shelf.c.name.label("shelf_name"),
            counted(frozenset(LIVE_STATES)).label("exported"),
            counted(_PENDING).label("pending"), counted(_FAILED).label("failed"),
            sa.func.max(record.c.exported_at).label("last_exported_at"),
        )
        .select_from(
            record.join(item, item.c.id == record.c.export_item_id)
            .join(dest, dest.c.id == record.c.shelf_destination_id)
            .join(target, target.c.id == dest.c.target_id)
            .join(shelf, shelf.c.id == dest.c.shelf_id)
        )
        .where(item.c.edition_id == edition_id, record.c.state != "recycled")
        .group_by(dest.c.id)
        .order_by(target.c.name, shelf.c.name, dest.c.id)
    ).all()  # fmt: skip
    return [
        EditionDestination(
            destination_id=str(r.id), target_id=str(r.target_id), target_name=str(r.target_name),
            target_kind=str(r.target_kind), shelf_name=str(r.shelf_name), enabled=bool(r.enabled),
            exported=int(r.exported or 0), pending=int(r.pending or 0),
            failed=int(r.failed or 0), last_exported_at=r.last_exported_at,
        )
        for r in rows
    ]  # fmt: skip


def _kepub_items(connection: sa.Connection, edition_id: str) -> int:
    item, record, content = export.export_item, export.export_record, storage.content
    live = sa.exists().where(
        record.c.export_item_id == item.c.id, record.c.state.in_(sorted(LIVE_STATES))
    )
    return int(
        connection.execute(
            sa.select(sa.func.count())
            .select_from(item.join(content, content.c.id == item.c.content_id))
            .where(item.c.edition_id == edition_id, content.c.format == "epub", live)
        ).scalar_one()
    )


def _blocks(connection: sa.Connection, edition_id: str) -> list[EditionBlock]:
    version, user = bundle.bundle_version, identity.user
    rows = connection.execute(
        sa.select(
            version.c.profile_user_id, user.c.username, version.c.part_index, version.c.number,
            version.c.size, version.c.created_at, version.c.last_used_at,
        )
        .select_from(version.join(user, user.c.id == version.c.profile_user_id))
        .where(version.c.edition_id == edition_id, version.c.purged_at.is_(None))
        .order_by(user.c.username, version.c.part_index, version.c.number)
    ).all()  # fmt: skip
    return [
        EditionBlock(
            profile_id=str(r.profile_user_id), username=str(r.username),
            part_index=int(r.part_index), number=int(r.number), size=int(r.size),
            created_at=r.created_at, last_used_at=r.last_used_at,
        )
        for r in rows
    ]  # fmt: skip
