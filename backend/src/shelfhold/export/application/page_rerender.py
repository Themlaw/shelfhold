"""Files already served by a destination that still hold a page to convert (EF-EXP-046, ADR-0054).

The export normalizes what it writes from now on; a chapter written before still has its AVIF
pages in the destination. `library normalize-pages` finds those files (names and the first bytes of
each page, no page decoded) and asks the same-path upgrade to render them again from the library:
the old file goes to the bin of its root, the new one takes its path, and a file that is already
clean is not touched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application.page_policy import PagePolicy
from shelfhold.export.application.snapshot import load_context
from shelfhold.infrastructure.archives import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.packaging.cbz import needs_normalization
from shelfhold.infrastructure.schema import export

__all__ = ["StaleFile", "stale_served_files"]


@dataclass(frozen=True, slots=True)
class StaleFile:
    destination_id: str
    export_item_id: str
    content_id: str
    local: str


def stale_served_files(
    engine: Engine, fsops: Fsops, policy_for: Callable[[str], PagePolicy | None]
) -> list[StaleFile]:
    """The served chapters (one Unit each) whose CBZ still holds a page the destination does not
    take. A file that is missing or unreadable is not this command's business."""
    record, item = export.export_record, export.export_item
    found: list[StaleFile] = []
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                record.c.shelf_destination_id, record.c.export_item_id, record.c.rel_path,
                item.c.content_id,
            )
            .select_from(record.join(item, item.c.id == record.c.export_item_id))
            .where(
                record.c.locator_type == "path",
                record.c.state.in_(("written", "scanned", "resolved")),
                item.c.kind == "unit",
                item.c.content_id.is_not(None),
                record.c.rel_path.like("%.cbz"),
            )
            .order_by(record.c.shelf_destination_id, record.c.rel_path)
        ).all()  # fmt: skip
        contexts: dict[str, Any] = {}
        for row in rows:
            ctx = contexts.get(row.shelf_destination_id)
            if ctx is None:
                ctx = contexts[row.shelf_destination_id] = load_context(
                    connection, row.shelf_destination_id
                )
            if ctx.root_local is None:
                continue
            policy = policy_for(ctx.target_id) or PagePolicy()
            local = ctx.local(row.rel_path)
            try:
                with (
                    fsops.open_read(local) as stream,
                    safe_open_archive(stream, name=row.rel_path) as archive,
                ):
                    stale = needs_normalization(archive, policy.keep)
            except (OSError, UnsafeArchiveError, ValueError, KeyError):
                continue
            if stale:
                found.append(
                    StaleFile(row.shelf_destination_id, row.export_item_id, row.content_id, local)
                )
    return found
