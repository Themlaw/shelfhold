"""Presence of a Unit (I20, EF-BIB-044): two predicates, never one.

`present_for_acquisition(unit)`: Shelfhold holds a readable copy that carries it, its own file or
a volume / item that covers it. The follow cycle and `backfill` reason on this one.

`present_in(unit, destination)`: a record of that destination, in a state that says the file is
there, belongs to an item that covers the Unit. Presence is per destination (fan-out is M4): a
Unit can be in one Kavita and not in another.

Fan-out (M4 L06a, ADR-0052 T9, T11): the line of a secondary indexer is a copy of the line of the
primary of its tree, so the two say the same of the file; a tree stays one copy for the purge and
for `independent_copies` (the replica is told once, by the primary). Only the screen of a shelf
(the route of the bindings) reads `present_units_in(edition, binding)`; the purge upstream reads
`independent_copies`, not this module.
"""

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import catalog, export
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


def present_units_in(
    connection: Connection, edition_id: str, destination_id: str
) -> frozenset[str]:
    """The Units of an Edition that this binding holds: `present_in` for each of them, so that the
    screen of the bindings of a shelf and the purge read the one predicate and never a second
    reading of the manifest."""
    units = connection.execute(
        sa.select(catalog.unit.c.id).where(catalog.unit.c.edition_id == edition_id)
    ).scalars()
    return frozenset(u for u in units if present_in(connection, u, destination_id))
