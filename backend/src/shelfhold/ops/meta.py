"""`meta rebuild|check` (ENF-OPS-020, EF-EXP-063): the projections of the database, written again.

`unit.json` and `work.json` in `/data/meta`, and the provenance mirror of every tree destination in
`<root>/.shelfhold/manifest/`. The database is the authority; none of them is read to learn
anything: a mirror line the database cannot explain stays as it is, flagged `unresolved`, and no
identity, Unit or row is ever created from an identifier it carries (the mirror is an untrusted
file). The command runs in the instance (the socket of the server, else the instance lock), and
the mirror is written under the write lease like any write to a library root, through the intent
journal. `check` writes nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from shelfhold.export.application.mirror import ProvenanceMirror
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import MetaProjection
from shelfhold.ops.errors import UnknownSubjectError
from shelfhold.ops.library import refusal_of

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["MetaOps"]


class MetaOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def _destinations(self) -> list[str]:
        table = export.shelf_destination
        with self._comp.engine.connect() as connection:
            return list(
                connection.execute(
                    sa.select(table.c.id).where(table.c.transport == "tree").order_by(table.c.id)
                ).scalars()
            )

    def _mirror(self, fsops: Any) -> ProvenanceMirror:
        return ProvenanceMirror(
            self._comp.engine, fsops, clock=self._comp.clock, operating_bus=self._comp.bus
        )

    def _projection(self) -> MetaProjection:
        return MetaProjection(self._comp.engine, self._comp.settings.data_dir)

    def _drifted_mirror_files(self) -> tuple[dict[str, list[str]], list[str]]:
        """Per destination, the mirror files missing or out of date, and the ones left alone
        (unreadable, a link, a newer major)."""
        destinations = self._destinations()
        if not destinations:
            return {}, []
        fsops, _loaded, _tombstones = self._comp.read_context()
        mirror = self._mirror(fsops)
        found = {d: names for d in destinations if (names := mirror.drifted(d))}
        return found, list(mirror.skipped)

    def check(self) -> dict[str, Any]:
        """What `rebuild` would write; nothing is."""
        try:
            report = self._projection().repair(dry_run=True)
            mirror, skipped = self._drifted_mirror_files()
        except Exception as error:
            refusal = refusal_of(error)
            if refusal is None:
                raise
            raise refusal from None
        return {
            "units": report.written, "units_read_only": report.read_only,
            "works": report.works_written, "works_read_only": report.works_read_only,
            "mirror": [name for names in mirror.values() for name in names],
            "mirror_skipped": skipped,
        }  # fmt: skip

    def rebuild(self, *, unit: str | None = None) -> dict[str, Any]:
        try:
            return self._rebuild(unit)
        except UnknownSubjectError:
            raise
        except Exception as error:
            refusal = refusal_of(error)
            if refusal is None:
                raise
            raise refusal from None

    def _rebuild(self, unit: str | None) -> dict[str, Any]:
        projection = self._projection()
        if unit is not None:
            if not is_ulid(unit) or projection.project(unit) == "missing_unit":
                raise UnknownSubjectError(f"no such Unit: {unit}")
            return {"units": [unit], "units_read_only": [], "works": [], "works_read_only": [],
                    "mirror": [], "mirror_skipped": []}  # fmt: skip
        report = projection.repair()
        mirror, skipped = self._drifted_mirror_files()
        written: list[str] = []
        if mirror:
            # a mirror file is a write to a library root: the lease, the intent, the fsops
            with self._comp.write_context() as w:
                writer = self._mirror(w.fsops)
                for destination_id in mirror:
                    written += writer.sync_destination(destination_id, writer=w.session)
                skipped = [*skipped, *writer.skipped]
        return {
            "units": report.written, "units_read_only": report.read_only,
            "works": report.works_written, "works_read_only": report.works_read_only,
            "mirror": written, "mirror_skipped": sorted(set(skipped)),
        }  # fmt: skip
