"""Thirty days of simulated activity for `stats` (EF-INT-025): rows written straight in the tables.

Per day `d` (1 to 30): one Unit detected `d` days before `now`, visible in Kavita one hour later.
Waiting states: 4 `target.not_indexed` and 2 `target.foreign_writer_detected` (both inside the
window), one more 40 days old. One sentinel guard. Two replays of "A verifier" resolved, one still
open, one resolved 40 days ago.
"""

from datetime import datetime, timedelta

import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, platform
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)
from tests.integration.test_schema_storage_export import (
    add_content,
    add_destination,
    add_export_item,
    add_record,
    add_shelf,
    add_target,
)


def _event(connection: sa.Connection, event_type: str, at: datetime) -> None:
    connection.execute(
        sa.insert(platform.event).values(
            type=event_type, schema_version=1, payload={}, occurred_at=at, visibility="ops"
        )
    )


def seed_thirty_days(settings: Settings, *, now: datetime) -> None:
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            work = add_work(connection)
            edition = add_edition(connection, work)
            binding = add_binding(connection, work, edition, add_source_item(connection))
            destination = add_destination(
                connection, add_shelf(connection), add_target(connection)
            )
            variants: list[str] = []
            for day in range(1, 31):
                detected = now - timedelta(days=day)
                unit = add_unit(connection, edition, str(day))
                variant = add_variant(connection, unit, binding, f"ch-{day}")
                variants.append(variant)
                connection.execute(
                    sa.update(catalog.unit_variant)
                    .where(catalog.unit_variant.c.id == variant)
                    .values(first_seen_at=detected)
                )
                content = add_content(
                    connection,
                    {"unit": unit, "variant": variant},
                    path=f"manga/ch-{day}.cbz",
                )
                item = add_export_item(connection, edition, content_id=content)
                add_record(
                    connection,
                    destination,
                    item,
                    rel_path=f"manga/S/S - {day}.cbz",
                    state="resolved",
                    exported_at=detected + timedelta(hours=1),
                )
            for _ in range(4):
                _event(connection, "target.not_indexed", now - timedelta(days=3))
            for _ in range(2):
                _event(connection, "target.foreign_writer_detected", now - timedelta(days=5))
            _event(connection, "target.not_indexed", now - timedelta(days=40))
            _event(connection, "mount.sentinel_missing", now - timedelta(days=2))
            _event(connection, "series.gap", now - timedelta(days=2))  # neither a guard nor a wait
            pending = [
                (2, "applied", now - timedelta(days=4)),
                (3, "applied", now - timedelta(days=6)),
                (4, "open", None),
                (5, "applied", now - timedelta(days=40)),
            ]
            for revision, state, resolved in pending:
                connection.execute(
                    sa.insert(acquisition.revision_pending).values(
                        id=new_ulid(),
                        variant_id=variants[0],
                        revision=revision,
                        nature="rewrite",
                        state=state,
                        resolved_at=resolved,
                    )
                )
    finally:
        engine.dispose()
