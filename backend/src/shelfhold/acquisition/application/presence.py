"""Presence of a Unit (I20, EF-BIB-044): two predicates, never one.

`present_for_acquisition(unit)`: Shelfhold holds a readable copy that carries it, its own file or
a volume / item that covers it. The follow cycle and `backfill` reason on this one.

`present_in(unit, destination)`: a record of that destination, in a state that says the file is
there, belongs to an item that covers the Unit. Presence is per destination (fan-out is M4): a
Unit can be in one Kavita and not in another.
"""

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import export
from shelfhold.library.public import has_readable_copy

# The record says the file is there: written by us, seen by the scan, resolved, or adopted.
RECORD_PRESENT = ("written", "scanned", "resolved", "adopted")


def present_for_acquisition(connection: Connection, unit_id: str) -> bool:
    return has_readable_copy(connection, unit_id)


def present_in(connection: Connection, unit_id: str, destination_id: str) -> bool:
    record, coverage = export.export_record, export.unit_coverage
    return (
        connection.execute(
            sa.select(sa.literal(1))
            .select_from(
                record.join(
                    coverage,
                    sa.and_(
                        coverage.c.carrier_type == "export_item",
                        coverage.c.carrier_id == record.c.export_item_id,
                    ),
                )
            )
            .where(
                coverage.c.unit_id == unit_id,
                record.c.shelf_destination_id == destination_id,
                record.c.state.in_(RECORD_PRESENT),
            )
            .limit(1)
        ).first()
        is not None
    )
