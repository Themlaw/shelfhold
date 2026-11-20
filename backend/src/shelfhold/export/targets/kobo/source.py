"""The Kobo folder for the host: what it is given and what it answers (EF-EXP-021, D19).

`KoboSource` is the `DestinationSource` of the fan-out for the targets of kind `kobo`: it reads the
items the host planned for a binding (the Editions filed on its shelf, admitted by the barrier,
served as an EPUB) and has the destination describe the tree of `.kepub.epub`. The destination
never reads the database and never touches the disk; this class is the only one that reads, and
what it gives the destination is plain values. It writes nothing either: the executor does, and
the manifest is kept by the fan-out.

Also here, for the bytes: `kepub`, the targets whose EPUB the host serves as a KEPUB — the
conversion and its cache are the host's (M5 L20b), made from the neutralized derivative, never
from the received original (I35).

A book that has a line in the manifest of this binding is **not described again**: the reader may
have taken the file away (a sync tool empties the folder as often as it fills it), so the line is
the memory of the export, as it is for a drop box.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.export.application.plan_executor import PlanBinding
from shelfhold.export.targets.kobo.destination import BookSpec, KoboDestination, SeriesSpec
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.sdk.api_call import ApiCall
from shelfhold.sdk.export_target import TargetCapabilities

__all__ = ["KIND", "KoboSource", "kobo_series"]

KIND = "kobo"
_KINDS = {"unit": False, "volume": True}  # the items that make a file; value: is it a volume


def kobo_series(connection: Connection, native_formats: Mapping[str, str]) -> list[SeriesSpec]:
    """The series of the books planned for a binding, in the order of their names. An item that
    is not a unit or a volume, or whose native format is not the EPUB a KEPUB is made from, makes
    no file here."""
    if not native_formats:
        return []
    item, coverage, unit = export.export_item, export.unit_coverage, catalog.unit
    edition, work = catalog.edition, catalog.work
    rows = connection.execute(
        sa.select(
            item.c.id,
            item.c.kind,
            item.c.frozen_attrs,
            coverage.c.position,
            unit.c.number_norm,
            unit.c.track,
            unit.c.title,
            work.c.title.label("work_title"),
        )
        .select_from(
            item.join(
                coverage,
                sa.and_(
                    coverage.c.carrier_type == "export_item", coverage.c.carrier_id == item.c.id
                ),
            )
            .join(unit, unit.c.id == coverage.c.unit_id)
            .join(edition, edition.c.id == item.c.edition_id)
            .join(work, work.c.id == edition.c.work_id)
        )
        .where(item.c.id.in_(sorted(native_formats)))
        .order_by(item.c.id, coverage.c.position)
    ).all()
    seen: set[str] = set()
    grouped: dict[str, list[BookSpec]] = {}
    for row in rows:
        if row.id in seen or row.kind not in _KINDS:
            continue
        seen.add(row.id)
        if native_formats[row.id] != "epub":
            continue  # a KEPUB is made from an EPUB; another format is another destination
        frozen = row.frozen_attrs or {}
        number = (
            (str(frozen.get("number_norm") or "") or row.number_norm)
            if _KINDS[row.kind]
            else row.number_norm
        )
        main = _KINDS[row.kind] or row.track == "main"
        grouped.setdefault(row.work_title, []).append(
            BookSpec(row.id, number if main else None, row.title or "", _KINDS[row.kind])
        )
    return [SeriesSpec(name, tuple(books)) for name, books in sorted(grouped.items())]


class KoboSource:
    """The `DestinationSource` of the targets of kind `kobo`. A target of another kind is not its:
    it answers nothing for it (another source does)."""

    def __init__(self, engine: Engine, *, destination: KoboDestination | None = None) -> None:
        self._engine = engine
        self._destination = destination or KoboDestination()

    def attach(self, host: Any) -> None:
        """Nothing to take from the host: the bytes are asked for by the executor, by item."""

    def _is_kobo(self, target_id: str) -> bool:
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(export.export_target.c.kind).where(
                        export.export_target.c.id == target_id
                    )
                ).scalar()
                == KIND
            )

    def capabilities(self, target_id: str) -> TargetCapabilities | None:
        return self._destination.capabilities if self._is_kobo(target_id) else None

    def kepub(self, target_id: str) -> bool:
        """The host serves each EPUB of this binding as a KEPUB (EF-EXP-021): the bytes of the
        neutralized derivative, converted by the rendition cache of M5 L20b."""
        return self._is_kobo(target_id)

    def plan(self, binding: PlanBinding) -> object | None:
        """The books the manifest of this binding does not hold yet, as one tree; None when there
        is nothing to write."""
        if not self._is_kobo(binding.target_id):
            return None
        epubs = sorted(i for i, fmt in binding.native_formats.items() if fmt == "epub")
        if not epubs:
            return None
        record = export.export_record
        with self._engine.connect() as connection:
            written = set(
                connection.execute(
                    sa.select(record.c.export_item_id).where(
                        record.c.shelf_destination_id == binding.destination_id,
                        record.c.export_item_id.in_(epubs),
                    )
                ).scalars()
            )
            series = kobo_series(connection, binding.native_formats)
        books = [
            SeriesSpec(s.name, tuple(b for b in s.books if b.item_id not in written))
            for s in series
        ]
        books = [s for s in books if s.books]
        return self._destination.describe(books) if books else None

    def calls(self, _binding: PlanBinding) -> tuple[object, ...]:
        return ()  # the reader looks at the folder itself: no call, no scan

    def sender(self, _binding: PlanBinding) -> Callable[[ApiCall], object]:
        def refuse(_call: ApiCall) -> object:
            raise ValueError("a Kobo folder sends no call")

        return refuse
