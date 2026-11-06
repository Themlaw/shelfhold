"""The sub-sources a connected service exposes, kept in `sub_source` (EF-SRC-021).

A sub-source is seen, not chosen: the row is created disabled (nothing is active on its own,
ENF-LEG-001) and what the service says later (name, language, warning, limit key) refreshes it,
while the admin's `enabled` is never touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.schema import acquisition
from shelfhold.sdk.source import SubSource

__all__ = ["upsert_sub_sources"]


def upsert_sub_sources(connection: sa.Connection, plugin_id: str, subs: Sequence[SubSource]) -> int:
    """Create or refresh one row per sub-source; the number of sub-sources seen."""
    table = acquisition.sub_source
    for sub in subs:
        values = {
            "name": sub.name,
            "lang": str(sub.lang),
            "content_warning": sub.content_warning,
            "limit_key": sub.limit_key,
        }
        connection.execute(
            sqlite_insert(table)
            .values(plugin_id=plugin_id, source_key=sub.source_key, enabled=False, **values)
            .on_conflict_do_update(index_elements=["plugin_id", "source_key"], set_=values)
        )
    return len(subs)
