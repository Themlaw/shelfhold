"""`shelfhold library assemble-volume` (EF-EXP-019, EF-BIB-044, M4 L10): the chapter files of an
Edition that a destination serves, from chapter `--first` to `--last`, become one volume.

Not a reserved action: nothing is deleted (the chapter files go to the bin of their root, thirty
days, and come back with `restore`), the progress of every member is kept, and an assembly cut off
is finished by the next start. The refusals are the barrier of the export (a paused destination, a
frozen root, a shelf that no longer admits the Edition) and the shape of the request."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from shelfhold.export.application.assembly import (
    AssemblyOutcome,
    AssemblyRequest,
    VolumeAssembly,
    served_chapters,
)
from shelfhold.export.application.rendering import cbz_renderer
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.packaging.cbz import PageTarget
from shelfhold.ops.errors import InvalidChoiceError
from shelfhold.ops.page_formats import _capabilities_of

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["VolumeAssemblyOps"]

MAX_CHAPTERS = 2000  # chapters under one volume: a typo (`1 100000`) must not read a library


def _number(text: str, name: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise InvalidChoiceError(f"{name} is a number") from None
    if not value.is_finite() or value < 0:
        raise InvalidChoiceError(f"{name} is a number, zero or more")
    return value


class VolumeAssemblyOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def assemble(
        self, destination_id: str, edition_id: str, volume: str, first: str, last: str
    ) -> dict[str, Any]:
        if not (is_ulid(destination_id) and is_ulid(edition_id)):
            raise InvalidChoiceError("a destination and an Edition are identifiers")
        low, high = _number(first, "first"), _number(last, "last")
        if high < low or high - low >= MAX_CHAPTERS:
            raise InvalidChoiceError(f"first <= last, at most {MAX_CHAPTERS} chapters")
        comp, settings = self._comp, self._comp.settings
        with comp.write_context() as w:
            with comp.engine.connect() as connection:
                items = served_chapters(connection, destination_id, edition_id, low, high)
            target = PageTarget(settings.page_target_format, settings.page_target_quality)
            assembly = VolumeAssembly(
                comp.engine, w.fsops, locate=w.loaded.locate, data_dir=settings.data_dir,
                clock=comp.clock, bus=w.bus,
                renderer=cbz_renderer(
                    w.fsops, staging_dir=settings.data_dir / "tmp", target=target,
                    capabilities=_capabilities_of(comp.engine),
                ),
            )  # fmt: skip
            result = assembly.assemble(AssemblyRequest(destination_id, tuple(items), volume))
        return {
            "outcome": result.outcome.value,
            "assembled": result.outcome is AssemblyOutcome.ASSEMBLED,
            "export_item_id": result.export_item_id,
            "chapters": len(items),
            "code": "volume.gap" if result.outcome is AssemblyOutcome.GAP else None,
            "missing": list(result.missing),
            "missing_text": ", ".join(result.missing),
        }
