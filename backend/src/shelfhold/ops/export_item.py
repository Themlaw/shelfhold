"""`export item show <id>` (EF-BIB-044, EF-LEC-014, M2 L26): what an export item carries and where
the file kept for it is.

A volume of a ranged source is one `export_item` that covers several Units, and its file is kept
under `/data/ranged/` as the source sent it. The display says the volume, how many chapters it
carries, whether it is closed (frozen) and where the kept file is, or why it is not shown (missing,
a link, outside the folder of the ranges). Only the identifier is read from the caller, and it is
a ULID: `locate_ranged` reads nothing of the file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from shelfhold.export.application.ranged_export import RangedExportError, locate_ranged
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.ops.errors import InvalidChoiceError, UnknownSubjectError

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["ExportItemOps"]


class ExportItemOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def show(self, item_id: str) -> dict[str, Any]:
        if not is_ulid(item_id):
            raise InvalidChoiceError("not an export item identifier")
        item, cov = export.export_item, export.unit_coverage
        with self._comp.engine.connect() as connection:
            row = connection.execute(
                sa.select(item.c.edition_id, item.c.kind, item.c.closed, item.c.frozen_attrs).where(
                    item.c.id == item_id
                )
            ).first()
            if row is None:
                raise UnknownSubjectError("no such export item")
            units = connection.execute(
                sa.select(sa.func.count()).where(
                    cov.c.carrier_type == "export_item", cov.c.carrier_id == item_id
                )
            ).scalar_one()
        label = (row.frozen_attrs or {}).get("volume_label")
        kept: str | None = None
        problem: str | None = None
        try:
            kept = str(locate_ranged(self._comp.settings.data_dir, item_id))
        except RangedExportError as error:
            problem = error.code
        return {
            "id": item_id, "edition_id": str(row.edition_id), "kind": str(row.kind),
            "label": label if isinstance(label, str) else None, "closed": bool(row.closed),
            "units": int(units), "kept": kept, "kept_problem": problem,
        }  # fmt: skip
