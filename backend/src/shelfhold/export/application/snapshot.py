"""What the planner reads: a `PlanRequest` loaded from the database, and its version.

The version of a plan (`base_version`, EF-EXP-042) is the digest of the *inputs* the planner
sees and that nothing but the world changes: the shelf, the binding, the Editions on the shelf,
their Units and the archives received for them. The manifest and the series are left out on
purpose: the pipeline itself writes them while it applies a plan, and a plan must not go stale
because of its own progress (concurrent writers are kept apart by the resource locks and by the
fingerprint of the files the plan touches).
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.export.application.rendering import comicinfo_sha256
from shelfhold.export.domain.naming import FrozenAttrs, NamingTemplate
from shelfhold.export.domain.path_map import PathEntry, PathMap, PathMode, clean_absolute
from shelfhold.export.domain.planner import (
    DestinationFacts,
    EditionFacts,
    PlanRequest,
    RecordFacts,
    SeriesFacts,
    UnitFacts,
)
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.library.public import ExportSource, export_sources

__all__ = ["DestinationContext", "LoadedRequest", "load_context", "load_request", "version_of"]


@dataclass(frozen=True)
class DestinationContext:
    destination_id: str
    shelf_id: str
    target_id: str
    transport: str
    egress: str
    root_subpath: str
    template: NamingTemplate
    max_age_rating: int | None
    retention_mode: str
    root_local: str | None  # where the tree starts on this machine
    remote_library_id: str | None
    path_map: PathMap

    def facts(self) -> DestinationFacts:
        return DestinationFacts(
            shelf_destination_id=self.destination_id, shelf_id=self.shelf_id,
            transport=self.transport, egress=self.egress, remote_library_id=self.remote_library_id,
            template=self.template, max_age_rating=self.max_age_rating,
        )  # fmt: skip

    def local(self, rel_path: str) -> str:
        if self.root_local is None:
            raise ValueError("this destination has no folder on this machine")
        return f"{self.root_local}/{rel_path}"

    def remote_folder(self, local_folder: str) -> str:
        return self.path_map.to_remote(local_folder)


@dataclass(frozen=True)
class LoadedRequest:
    request: PlanRequest
    version: int


def _path_map(entries: list[dict[str, str]]) -> PathMap:
    return PathMap(
        tuple(PathEntry(e["local"], e["remote"], PathMode(e.get("mode", "rw"))) for e in entries)
    )


def load_context(connection: Connection, destination_id: str) -> DestinationContext:
    d, s, t = export.shelf_destination, export.shelf, export.export_target
    row = connection.execute(
        sa.select(d, s.c.max_age_rating, s.c.retention_mode, t.c.path_map)
        .select_from(d.join(s, s.c.id == d.c.shelf_id).join(t, t.c.id == d.c.target_id))
        .where(d.c.id == destination_id)
    ).one()
    path_map = _path_map(row.path_map)
    writable = path_map.writable()
    root = None
    if writable:
        base = writable[0].local
        root = clean_absolute(f"{base}/{row.root_subpath}") if row.root_subpath else base
    library = connection.execute(
        sa.select(export.shelf_destination_remote_library.c.remote_library_id)
        .where(export.shelf_destination_remote_library.c.shelf_destination_id == destination_id)
        .order_by(export.shelf_destination_remote_library.c.remote_library_id)
    ).scalar()
    return DestinationContext(
        destination_id=destination_id, shelf_id=row.shelf_id, target_id=row.target_id,
        transport=row.transport, egress=row.egress, root_subpath=row.root_subpath,
        template=NamingTemplate.loads(row.template), max_age_rating=row.max_age_rating,
        retention_mode=row.retention_mode, root_local=root, remote_library_id=library,
        path_map=path_map,
    )  # fmt: skip


def _within_any(path: str, bases: frozenset[str]) -> bool:
    return any(path == b or path.startswith(b.rstrip("/") + "/") for b in bases)


def load_request(
    connection: Connection,
    ctx: DestinationContext,
    *,
    exists: Callable[[str], bool],
    covered_locals: frozenset[str],
    segment_policy_confirmed: bool,
) -> LoadedRequest:
    editions, unit_rows, sources = _inputs(connection, ctx)
    records = _records(connection, ctx.destination_id)
    units = []
    for u in unit_rows:
        source = sources.get(u.id)
        record = records.get(u.id)
        ready = source is not None and source.local_path is not None
        sidecar = (
            comicinfo_sha256(connection, u.id, record.frozen.series) if record and ready else None
        )
        units.append(
            UnitFacts(
                unit_id=u.id, edition_id=u.edition_id, number_norm=u.number_norm, track=u.track,
                part=int(u.part) if u.part.isdigit() and int(u.part) >= 1 else 1,
                segment=u.number_segment, source_order=u.source_order,
                content_hash=source.content_hash if source and ready else None,
                sidecar_sha256=sidecar, watermarked=bool(source and source.watermarked),
            )
        )  # fmt: skip
    series, edition_series, ceilings = _series(connection, ctx, {e.edition_id for e in editions})
    root = ctx.root_local
    request = PlanRequest(
        destination=ctx.facts(), editions=editions, units=tuple(units),
        records=tuple(records.values()), series=series, edition_series=edition_series,
        occupied=(lambda rel: root is not None and exists(f"{root}/{rel}")),
        covered=(lambda rel: root is not None and _within_any(f"{root}/{rel}", covered_locals)),
        library_ceilings=ceilings, segment_policy_confirmed=segment_policy_confirmed,
    )  # fmt: skip
    return LoadedRequest(request, _version(ctx, editions, unit_rows, sources))


def _inputs(
    connection: Connection, ctx: DestinationContext
) -> tuple[tuple[EditionFacts, ...], list[sa.Row[Any]], dict[str, ExportSource]]:
    edition, work, unit = catalog.edition, catalog.work, catalog.unit
    editions = tuple(
        EditionFacts(r.id, r.work_id, r.title, r.lang, r.text_version)
        for r in connection.execute(
            sa.select(
                edition.c.id, edition.c.work_id, edition.c.lang, edition.c.text_version,
                work.c.title,
            )
            .select_from(edition.join(work, work.c.id == edition.c.work_id))
            .where(edition.c.shelf_id == ctx.shelf_id)
            .order_by(edition.c.id)
        )
    )  # fmt: skip
    unit_rows = list(
        connection.execute(
            sa.select(unit)
            .where(unit.c.edition_id.in_([e.edition_id for e in editions]))
            .order_by(unit.c.id)
        )
    )
    return editions, unit_rows, export_sources(connection, [u.id for u in unit_rows])


def version_of(connection: Connection, ctx: DestinationContext) -> int:
    """The version of the plan inputs now: cheap, no manifest and no ComicInfo."""
    editions, unit_rows, sources = _inputs(connection, ctx)
    return _version(ctx, editions, unit_rows, sources)


def _records(connection: Connection, destination_id: str) -> dict[str, RecordFacts]:
    record, item, coverage = export.export_record, export.export_item, export.unit_coverage
    rows = connection.execute(
        sa.select(record, item.c.frozen_attrs, coverage.c.unit_id)
        .select_from(
            record.join(item, item.c.id == record.c.export_item_id).join(
                coverage,
                sa.and_(
                    coverage.c.carrier_type == "export_item", coverage.c.carrier_id == item.c.id
                ),
            )
        )
        .where(record.c.shelf_destination_id == destination_id)
    )
    return {
        r.unit_id: RecordFacts(
            export_item_id=r.export_item_id, unit_id=r.unit_id, state=r.state, rel_path=r.rel_path,
            content_hash=r.content_hash, sidecar_sha256=r.sidecar_sha256,
            frozen=FrozenAttrs(**r.frozen_attrs),
        )
        for r in rows
    }  # fmt: skip


def _series(
    connection: Connection, ctx: DestinationContext, edition_ids: set[str]
) -> tuple[tuple[SeriesFacts, ...], dict[str, str], frozenset[int | None]]:
    library = ctx.remote_library_id
    if library is None:
        return (), {}, frozenset()
    s, link, edition = export.export_series, export.edition_export_series, catalog.edition
    works: dict[str, set[str]] = defaultdict(set)
    editions: dict[str, set[str]] = defaultdict(set)
    mine: dict[str, str] = {}
    for r in connection.execute(
        sa.select(link.c.export_series_id, link.c.edition_id, edition.c.work_id)
        .join(edition, edition.c.id == link.c.edition_id)
        .where(link.c.remote_library_id == library)
    ):
        works[r.export_series_id].add(r.work_id)
        editions[r.export_series_id].add(r.edition_id)
        if r.edition_id in edition_ids:
            mine[r.edition_id] = r.export_series_id
    series = tuple(
        SeriesFacts(
            r.id, r.remote_library_id, r.name_raw, r.normalized_name,
            frozenset(works[r.id]), frozenset(editions[r.id]),
        )
        for r in connection.execute(
            sa.select(s).where(s.c.remote_library_id == library).order_by(s.c.id)
        )
    )  # fmt: skip
    d, sd, shelf = export.shelf_destination_remote_library, export.shelf_destination, export.shelf
    ceilings = frozenset(
        connection.execute(
            sa.select(shelf.c.max_age_rating)
            .select_from(
                d.join(sd, sd.c.id == d.c.shelf_destination_id).join(
                    shelf, shelf.c.id == sd.c.shelf_id
                )
            )
            .where(d.c.remote_library_id == library)
        ).scalars()
    )
    return series, mine, ceilings


def _version(
    ctx: DestinationContext,
    editions: tuple[EditionFacts, ...],
    unit_rows: list[sa.Row[Any]],
    sources: dict[str, ExportSource],
) -> int:
    def unit_material(u: sa.Row[Any]) -> list[object]:
        source = sources.get(u.id)
        return [
            u.id, u.edition_id, u.number_norm, u.track, u.part, u.number_segment, u.source_order,
            source.content_hash if source and source.local_path else None,
            bool(source and source.watermarked),
        ]  # fmt: skip

    material = {
        "destination": asdict(ctx.facts()) | {"template": ctx.template.dumps()},
        "retention": ctx.retention_mode,
        "root": ctx.root_local,
        "editions": [asdict(e) for e in editions],
        "units": [unit_material(u) for u in unit_rows],
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).digest()
    return int.from_bytes(digest[:6], "big")  # 48 bits: fits an integer column and an ETag
