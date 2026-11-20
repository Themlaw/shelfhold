"""The CWA drop box for the host: what it is given and what it answers (EF-EXP-022).

`CwaSource` is the `DestinationSource` of the fan-out for the targets of kind `cwa`: it reads the
items the host planned for a binding (the Editions filed on its shelf, admitted by the barrier,
served as an EPUB) and has the destination describe the drop. The destination never reads the
database and never touches the disk; this class is the only one that reads, and what it gives the
destination is plain values. It writes nothing either: the executor does, and the manifest is
kept by the fan-out.

A book that has a line in the manifest of this binding is **not described again**, whether its
file is still in the folder or the ingester already took it: the file disappears after ingestion,
so the line (typed by `idempotency_key`) is the only memory of the drop.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from shelfhold_sdk.api_call import ApiCall
from shelfhold_sdk.export_target import TargetCapabilities
from sqlalchemy import Connection, Engine

from shelfhold.export.application.plan_executor import PlanBinding
from shelfhold.export.targets.cwa.destination import CwaDestination, DropSpec
from shelfhold.infrastructure.schema import catalog, export

__all__ = ["KIND", "CwaSource"]

KIND = "cwa"


def _titles(connection: Connection, item_ids: list[str]) -> dict[str, str]:
    item, edition, work = export.export_item, catalog.edition, catalog.work
    rows = connection.execute(
        sa.select(item.c.id, work.c.title)
        .select_from(
            item.join(edition, edition.c.id == item.c.edition_id).join(
                work, work.c.id == edition.c.work_id
            )
        )
        .where(item.c.id.in_(item_ids))
    )
    return {r.id: r.title for r in rows}


class CwaSource:
    """The `DestinationSource` of the targets of kind `cwa`. A target of another kind is not its:
    it answers nothing for it (another source does)."""

    def __init__(self, engine: Engine, *, destination: CwaDestination | None = None) -> None:
        self._engine = engine
        self._destination = destination or CwaDestination()

    def attach(self, host: Any) -> None:
        """Nothing to take from the host: the bytes are asked for by the executor, by item."""

    def _is_cwa(self, target_id: str) -> bool:
        with self._engine.connect() as connection:
            kind = connection.execute(
                sa.select(export.export_target.c.kind).where(export.export_target.c.id == target_id)
            ).scalar()
        return bool(kind == KIND)

    def capabilities(self, target_id: str) -> TargetCapabilities | None:
        return self._destination.capabilities if self._is_cwa(target_id) else None

    def sidecars(self, binding: PlanBinding) -> frozenset[str]:
        return frozenset()  # a drop box takes the EPUB and nothing else

    def plan(self, binding: PlanBinding) -> object | None:
        """The books the manifest of this binding does not hold yet, as one plan; None when there
        is nothing to drop."""
        if not self._is_cwa(binding.target_id):
            return None
        epubs = sorted(i for i, fmt in binding.native_formats.items() if fmt == "epub")
        if not epubs:
            return None
        record = export.export_record
        with self._engine.connect() as connection:
            dropped = set(
                connection.execute(
                    sa.select(record.c.export_item_id).where(
                        record.c.shelf_destination_id == binding.destination_id,
                        record.c.export_item_id.in_(epubs),
                    )
                ).scalars()
            )
            titles = _titles(connection, [i for i in epubs if i not in dropped])
        books = [DropSpec(i, titles.get(i, "")) for i in epubs if i not in dropped]
        return self._destination.describe(books) if books else None

    def calls(self, binding: PlanBinding) -> tuple[object, ...]:
        return ()  # the ingester watches its folder: no call, no scan

    def sender(self, binding: PlanBinding) -> Any:
        def refuse(call: ApiCall) -> object:
            raise ValueError("a drop box sends no call")

        return refuse
