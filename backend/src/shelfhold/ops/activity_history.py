"""The activity history and its blocklist, for the administrator (EF-INT-019).

The history is the activity page without its window: the same rows, the same one filter
(`project_event`, which calls `activity_view`), read by the administrator and paged back in time.
Nothing reaches the answer that did not go through that filter, and of what goes through only a
closed list of fields is kept (`CLEAN_KEYS`): a path, a host, an address or a key that an event
payload happened to carry is dropped here, not trusted to be absent.

The blocklist's removal is audited in the transaction that makes it: nothing lifts a block in
silence.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.acquisition.application.blocklist import (
    Block,
    block_unit,
    find_block,
    get_block,
    unblock,
)
from shelfhold.events.activity import Viewer
from shelfhold.events.projection import project_event
from shelfhold.identity.public import AuditEntry, Principal, audit_actor, audit_insert, reader_scope
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import acquisition, catalog, platform
from shelfhold.ops.activity_feed import activity_event_of

__all__ = [
    "CLEAN_KEYS",
    "BlockView",
    "HistoryEntry",
    "HistoryPage",
    "activity_history",
    "describe_blocks",
    "record_block",
    "remote_id_of",
    "remove_block",
]

# What of an event payload the history shows: counts, codes and identifiers of the household's own
# things. Anything else is dropped, whatever it holds.
CLEAN_KEYS = frozenset(
    {
        "count", "state", "reason", "code", "kind", "number", "work_id", "series_id",
        "edition_id", "unit_id", "request_id", "source_key",
    }
)  # fmt: skip
_TOKEN = re.compile(r"[A-Za-z0-9_:-]{1,64}")  # no slash, no dot, no space: no path, no host


@dataclass(frozen=True)
class HistoryEntry:
    event_id: int
    type: str
    visibility: str
    occurred_at: datetime
    title: str | None
    title_masked: bool
    payload: dict[str, Any]


@dataclass(frozen=True)
class HistoryPage:
    items: list[HistoryEntry]
    next_before: int | None


def clean_payload(payload: Mapping[str, object]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in CLEAN_KEYS:
            continue
        if isinstance(value, bool | int) or (isinstance(value, str) and _TOKEN.fullmatch(value)):
            kept[key] = value
    return kept


def activity_history(
    engine: Engine, principal: Principal, *, limit: int, before: int | None
) -> HistoryPage:
    """The newest `limit` entries older than event `before`. The viewer is the principal's own
    profile (the administrator's: this is only called for it)."""
    table = platform.event
    query = sa.select(table).order_by(table.c.id.desc())
    if before is not None:
        query = query.where(table.c.id < before)
    items: list[HistoryEntry] = []
    last: int | None = None
    with engine.connect() as connection:
        scope = reader_scope(connection, principal.user_id)
        viewer = Viewer(principal.user_id, principal.role)
        rows = connection.execute(query.limit(limit + 1)).all()
        more = len(rows) > limit
        for row in rows[:limit]:
            last = row.id
            item = project_event(connection, viewer, scope, activity_event_of(connection, row))
            if item is None:  # not the viewer's to see: it leaves no trace
                continue
            items.append(
                HistoryEntry(
                    event_id=int(row.id), type=item.type, visibility=item.visibility,
                    occurred_at=row.occurred_at, title=item.title,
                    title_masked=item.title_masked, payload=clean_payload(item.payload),
                )
            )  # fmt: skip
    return HistoryPage(items, last if more else None)


def remove_block(
    engine: Engine, principal: Principal, block_id: str, *, now: datetime
) -> Block | None:
    """Lift a block and write who did it, in one transaction. None: there is no such block."""
    with write_transaction(engine) as connection:
        found = get_block(connection, block_id)
        if found is None:
            return None
        unblock(connection, block_id)
        connection.execute(
            audit_insert(
                AuditEntry(
                    actor_id=audit_actor(principal),
                    action="blocklist.remove",
                    at=now,
                    target=f"block:{block_id}",
                    session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )
        )
        return found


def record_block(
    engine: Engine,
    principal: Principal,
    *,
    source_key: str,
    remote_unit_id: str,
    reason: str,
    now: datetime,
) -> tuple[Block, bool]:
    """Block a chapter and write who did it. The flag: it was new (one already blocked is
    returned as it was, and nothing more is written)."""
    with write_transaction(engine) as connection:
        created = find_block(connection, source_key, remote_unit_id) is None
        block = block_unit(
            connection, source_key=source_key, remote_unit_id=remote_unit_id, reason=reason,
            actor_id=principal.user_id, now=now,
        )  # fmt: skip
        if created:
            connection.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=audit_actor(principal),
                        action="blocklist.add",
                        at=now,
                        target=f"block:{block.id}",
                        session_id=principal.session_id,
                        auth_time=principal.auth_time,
                    )
                )
            )
        return block, created


@dataclass(frozen=True)
class BlockView:
    """A block as the administrator reads it: which chapter (by its title and number when it is
    known here) and how often it was offered again. The source's own identifier is shown as a
    short digest only: it can be an address of the source, and nothing here names one."""

    block: Block
    ref: str
    title: str | None
    number: str | None


def _digest(remote_unit_id: str) -> str:
    return hashlib.sha256(remote_unit_id.encode()).hexdigest()[:12]


def describe_blocks(engine: Engine, blocks: list[Block]) -> list[BlockView]:
    variant, binding, item = (
        catalog.unit_variant,
        acquisition.source_binding,
        acquisition.source_item,
    )
    views: list[BlockView] = []
    with engine.connect() as connection:
        for block in blocks:
            found = connection.execute(
                sa.select(catalog.unit.c.number_raw, catalog.work.c.title)
                .select_from(
                    variant.join(binding, binding.c.id == variant.c.source_binding_id)
                    .join(item, item.c.id == binding.c.source_item_id)
                    .join(catalog.unit, catalog.unit.c.id == variant.c.unit_id)
                    .join(catalog.edition, catalog.edition.c.id == catalog.unit.c.edition_id)
                    .join(catalog.work, catalog.work.c.id == catalog.edition.c.work_id)
                )
                .where(
                    item.c.source_key == block.source_key,
                    variant.c.remote_unit_id == block.remote_unit_id,
                )
                .limit(1)
            ).first()  # fmt: skip
            views.append(
                BlockView(
                    block, _digest(block.remote_unit_id),
                    found.title if found else None, found.number_raw if found else None,
                )
            )  # fmt: skip
    return views


def remote_id_of(engine: Engine, unit_id: str, source_key: str) -> str | None:
    """What `source_key` calls the chapter of our Unit `unit_id`, when it ever offered it."""
    variant, binding, item = (
        catalog.unit_variant,
        acquisition.source_binding,
        acquisition.source_item,
    )
    with engine.connect() as connection:
        return connection.execute(
            sa.select(variant.c.remote_unit_id)
            .select_from(
                variant.join(binding, binding.c.id == variant.c.source_binding_id).join(
                    item, item.c.id == binding.c.source_item_id
                )
            )
            .where(variant.c.unit_id == unit_id, item.c.source_key == source_key)
            .limit(1)
        ).scalar()
