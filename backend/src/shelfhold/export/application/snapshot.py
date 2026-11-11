"""What the planner reads: a `PlanRequest` loaded from the database, and its version.

The version of a plan (`base_version`, EF-EXP-042) is a 128-bit digest (hex) of everything the
planner sees: the shelf, the binding, the Editions on the shelf, their Units and the archives
received for them (the *inputs*), then the manifest of the destination and the series of its
remote library, whichever shelf created them. It is compared at the claim of the plan. While the
plan is applied only the inputs are compared again (`inputs_version_of`): the manifest and the
series are what the plan itself writes, and a plan must not go stale because of its own progress;
a series another shelf takes meanwhile is caught by the library lock and the reservation.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.public import BOOK_KINDS, Admission, RatingService
from shelfhold.export.application.foreign_names import load_foreign_names
from shelfhold.export.application.intents import within_any
from shelfhold.export.application.rendering import comicinfo_sha256
from shelfhold.export.application.target_state import (
    DestinationPausedError,
    ensure_active,
    load_health,
)
from shelfhold.export.domain.bindings import PARTITIONED, effective_partitioning, is_family
from shelfhold.export.domain.kavita_compat import TargetMode, blocks_family_shelf
from shelfhold.export.domain.naming import FrozenAttrs, NamingTemplate
from shelfhold.export.domain.path_map import PathEntry, PathMap, PathMode, clean_absolute
from shelfhold.export.domain.planner import (
    AGE_PAUSES,
    GATE_CLOSED,
    AgeGateFacts,
    DestinationFacts,
    EditionFacts,
    PlanRequest,
    RecordFacts,
    SeriesFacts,
    UnitFacts,
    age_rank,
)
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.library.public import ExportSource, epub_units, export_sources
from shelfhold.sdk.age import X_MIN_AGE, AgeRating

_HOLDING = ("written", "scanned", "resolved")  # a record whose file is in the library

__all__ = [
    "DestinationContext",
    "LoadedRequest",
    "admissions_of",
    "barrier_holds",
    "edition_age",
    "inputs_version_of",
    "load_context",
    "load_request",
    "paused_series",
    "unknown_exposed",
    "version_of",
]


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
    partitioning: str = "library"  # how the target keeps accounts apart (EF-EXP-047)
    access_control_known_bad: bool = False  # a family shelf is blocked on it (ENF-COMP-001)
    library_type: str | None = None  # `kavita_library_type`, declared by the admin (D19)

    def facts(self) -> DestinationFacts:
        return DestinationFacts(
            shelf_destination_id=self.destination_id, shelf_id=self.shelf_id,
            transport=self.transport, egress=self.egress, remote_library_id=self.remote_library_id,
            template=self.template, max_age_rating=self.max_age_rating,
            partitioning=self.partitioning,
            access_control_known_bad=self.access_control_known_bad,
            library_type=self.library_type,
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
    version: str


def _path_map(entries: list[dict[str, str]]) -> PathMap:
    return PathMap(
        tuple(PathEntry(e["local"], e["remote"], PathMode(e.get("mode", "rw"))) for e in entries)
    )


def load_context(connection: Connection, destination_id: str) -> DestinationContext:
    d, s, t = export.shelf_destination, export.shelf, export.export_target
    row = connection.execute(
        sa.select(
            d,
            s.c.max_age_rating,
            s.c.retention_mode,
            t.c.path_map,
            t.c.kind,
            t.c.access_partitioning,
            t.c.access_partitioning_attested_at,
            t.c.version_seen,
        )
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
        partitioning=effective_partitioning(
            row.kind, row.access_partitioning,
            attested=row.access_partitioning_attested_at is not None,
        ),
        access_control_known_bad=blocks_family_shelf(row.version_seen),
        library_type=row.kavita_library_type,
    )  # fmt: skip


def load_request(
    connection: Connection,
    ctx: DestinationContext,
    *,
    exists: Callable[[str], bool],
    covered_locals: frozenset[str],
    segment_policy_confirmed: bool,
    age_gate: AgeGateFacts = GATE_CLOSED,
) -> LoadedRequest:
    """`age_gate`: what Kavita said of the series ages just before (the export job reads it);
    a preview, which asks Kavita nothing, plans every gated EPUB as waiting."""
    editions, unit_rows, sources = _inputs(connection, ctx)
    admissions = admissions_of(connection, ctx, editions)
    records = _records(connection, ctx.destination_id)
    epubs = epub_units(connection, [u.id for u in unit_rows])
    units = []
    for u in unit_rows:
        source = sources.get(u.id)
        record = records.get(u.id)
        ready = source is not None and source.local_path is not None
        sidecar = (  # an EPUB has no ComicInfo: its OPF edit is what the record keeps
            comicinfo_sha256(connection, u.id, record.frozen.series)
            if record and ready and u.id not in epubs
            else None
        )
        units.append(
            UnitFacts(
                unit_id=u.id, edition_id=u.edition_id, number_norm=u.number_norm, track=u.track,
                part=int(u.part) if u.part.isdigit() and int(u.part) >= 1 else 1,
                segment=u.number_segment, source_order=u.source_order,
                content_hash=source.content_hash if source and ready else None,
                sidecar_sha256=sidecar, watermarked=bool(source and source.watermarked),
                format="epub" if u.id in epubs else "cbz",
            )
        )  # fmt: skip
    gated = sorted({u.edition_id for u in unit_rows if u.id in epubs})
    series, edition_series, ceilings = _series(connection, ctx, {e.edition_id for e in editions})
    root = ctx.root_local
    request = PlanRequest(
        destination=ctx.facts(), editions=editions, units=tuple(units),
        records=tuple(records.values()), series=series, edition_series=edition_series,
        occupied=(lambda rel: root is not None and exists(f"{root}/{rel}")),
        covered=(lambda rel: root is not None and within_any(f"{root}/{rel}", covered_locals)),
        library_ceilings=ceilings, segment_policy_confirmed=segment_policy_confirmed,
        paused_series=paused_series(connection, ctx.destination_id),
        foreign_names=load_foreign_names(connection, ctx.destination_id),
        admissions=admissions, holds_books=_holds_books(connection, ctx, editions),
        age_wanted=wanted_editions(connection, gated),
        age_unknown_exposed=unknown_exposed(connection, gated, age_gate.unknowns_tier),
        age_seeded=seeded_series(connection, ctx.destination_id) if gated else frozenset(),
        age_gate=age_gate,
    )  # fmt: skip
    return LoadedRequest(
        request,
        _version(
            ctx, editions, unit_rows, sources, _world(records, series, edition_series), admissions
        ),
    )


def edition_age(
    connection: Connection, edition_id: str, *, rating: RatingService | None = None
) -> AgeRating | None:
    """The age an admin fixed for the Edition, else the class of its Work; None if nobody rated."""
    edition = catalog.edition
    row = connection.execute(
        sa.select(edition.c.work_id, edition.c.age_rating, edition.c.age_x).where(
            edition.c.id == edition_id
        )
    ).one()
    if row.age_rating is not None or row.age_x:
        return AgeRating(X_MIN_AGE, x=True) if row.age_x else AgeRating(int(row.age_rating))
    return (rating or RatingService()).effective_age_of(connection, row.work_id)


def wanted_editions(connection: Connection, edition_ids: list[str]) -> frozenset[str]:
    """The Editions whose series has an age to push: rated, by themselves or by their Work."""
    rating = RatingService()
    return frozenset(
        e for e in edition_ids if edition_age(connection, e, rating=rating) is not None
    )


def unknown_exposed(
    connection: Connection, edition_ids: list[str], unknowns_tier: int | None
) -> frozenset[str]:
    """The Editions whose first EPUB an account that sees the "unknown" series could read: their
    age is above the lowest tier of such an account (`AgeGateFacts.unknowns_tier`)."""
    if unknowns_tier is None:
        return frozenset()
    found = set()
    for edition_id in edition_ids:
        age = edition_age(connection, edition_id)
        if age is not None and age_rank(age) > unknowns_tier:
            found.add(edition_id)
    return frozenset(found)


def seeded_series(connection: Connection, destination_id: str) -> frozenset[str]:
    """The export series of the destination that hold a file already (written or further on)."""
    record, item, link = export.export_record, export.export_item, export.edition_export_series
    ctx = load_context(connection, destination_id)
    if ctx.remote_library_id is None:
        return frozenset()
    return frozenset(
        connection.execute(
            sa.select(link.c.export_series_id)
            .select_from(
                record.join(item, item.c.id == record.c.export_item_id).join(
                    link,
                    sa.and_(
                        link.c.edition_id == item.c.edition_id,
                        link.c.remote_library_id == ctx.remote_library_id,
                    ),
                )
            )
            .where(
                record.c.shelf_destination_id == destination_id,
                record.c.state.in_(_HOLDING),
            )
            .distinct()
        ).scalars()
    )


def _holds_books(
    connection: Connection, ctx: DestinationContext, editions: tuple[EditionFacts, ...]
) -> bool:
    """Does the shelf take book Works (D19)? By its default kinds, or by a Work filed on it."""
    kinds = connection.execute(
        sa.select(export.shelf.c.default_kinds).where(export.shelf.c.id == ctx.shelf_id)
    ).scalar()
    if any(k in BOOK_KINDS for k in kinds or ()):
        return True
    works = sorted({e.work_id for e in editions})
    if not works:
        return False
    found = connection.execute(
        sa.select(catalog.work.c.id)
        .where(catalog.work.c.id.in_(works), catalog.work.c.kind.in_(sorted(BOOK_KINDS)))
        .limit(1)
    ).first()
    return found is not None


def barrier_holds(connection: Connection, destination_id: str, edition_id: str) -> bool:
    """The last look before a write: read again now, not from the plan, whether the shelf below
    18+ (the destination partitions, Kavita has no known flaw) takes this Edition. An adult shelf
    has nothing to ask. A plan that waited, a record put back to `planned`, a rating that moved:
    none of them gets a file into the library of the children."""
    ctx = load_context(connection, destination_id)
    try:
        # a pause set since the run began (a ceiling changed, an audit, a reinstalled server) is
        # read here, under the lock, whatever the ceiling has become
        ensure_active(connection, destination_id)
    except DestinationPausedError:
        return False
    if not is_family(ctx.max_age_rating):
        return True
    if ctx.partitioning not in PARTITIONED or ctx.access_control_known_bad:
        return False
    if load_health(connection, ctx.target_id).mode is TargetMode.DEGRADED_READONLY:
        return False  # preview only: a write of any kind stays out of the library of the children
    shelved = connection.execute(
        sa.select(catalog.edition.c.shelf_id).where(catalog.edition.c.id == edition_id)
    ).scalar()
    if shelved != ctx.shelf_id:
        return False  # an Edition withdrawn (or never filed here) writes nothing into this shelf
    verdict = RatingService().admission_of(connection, edition_id, shelf_id=ctx.shelf_id)
    return verdict is Admission.ALLOWED


def admissions_of(
    connection: Connection, ctx: DestinationContext, editions: tuple[EditionFacts, ...]
) -> dict[str, str]:
    """Who may enter a shelf below 18+, Edition by Edition: the verdict of `RatingService`, the one
    rule `relink` and `rate` use. Empty for an adult shelf, which has nothing to ask."""
    if not is_family(ctx.max_age_rating):
        return {}
    rating = RatingService()
    return {
        e.edition_id: rating.admission_of(connection, e.edition_id, shelf_id=ctx.shelf_id).value
        for e in editions
    }


def paused_series(connection: Connection, destination_id: str) -> frozenset[str]:
    """The series a foreign writer paused: nothing new is written into them (EF-EXP-043)."""
    series = export.export_series
    return frozenset(
        connection.execute(
            sa.select(series.c.id).where(
                series.c.shelf_destination_id == destination_id,
                series.c.paused_reason.isnot(None),
                series.c.paused_reason.not_in(sorted(AGE_PAUSES)),  # the age gate holds those
            )
        ).scalars()
    )


def _inputs(
    connection: Connection, ctx: DestinationContext
) -> tuple[tuple[EditionFacts, ...], list[sa.Row[Any]], dict[str, ExportSource]]:
    unit = catalog.unit
    editions = edition_facts(connection, ctx.shelf_id)
    unit_rows = list(
        connection.execute(
            sa.select(unit)
            .where(unit.c.edition_id.in_([e.edition_id for e in editions]))
            .order_by(unit.c.id)
        )
    )
    return (
        editions,
        unit_rows,
        export_sources(connection, [u.id for u in unit_rows], formats=("cbz", "epub")),
    )


def edition_facts(connection: Connection, shelf_id: str) -> tuple[EditionFacts, ...]:
    """The Editions filed on a shelf, with the Series above their Work when there is one: the
    Kavita series is the Series if present, else the Work (EF-BIB-025)."""
    edition, work = catalog.edition, catalog.work
    member, series = catalog.series_member, catalog.series
    found: dict[str, EditionFacts] = {}
    for r in connection.execute(
        sa.select(
            edition.c.id,
            edition.c.work_id,
            edition.c.lang,
            edition.c.text_version,
            edition.c.packaging,
            work.c.title,
            series.c.id.label("series_id"),
            series.c.title.label("series_title"),
        )
        .select_from(
            edition.join(work, work.c.id == edition.c.work_id)
            .outerjoin(member, member.c.work_id == work.c.id)
            .outerjoin(series, series.c.id == member.c.series_id)
        )
        .where(edition.c.shelf_id == shelf_id)
        .order_by(edition.c.id, series.c.id)
    ):  # a Work in two Series would be listed twice: the first, by id, names it
        found.setdefault(
            r.id,
            EditionFacts(
                r.id,
                r.work_id,
                r.title,
                r.lang,
                r.text_version,
                r.series_id,
                r.series_title,
                r.packaging,
            ),
        )
    return tuple(found.values())


def version_of(connection: Connection, ctx: DestinationContext) -> str:
    """The full version now: inputs, manifest of the destination, series of the library."""
    editions, unit_rows, sources = _inputs(connection, ctx)
    records = _records(connection, ctx.destination_id)
    series, edition_series, _ = _series(connection, ctx, {e.edition_id for e in editions})
    return _version(
        ctx, editions, unit_rows, sources, _world(records, series, edition_series),
        admissions_of(connection, ctx, editions),
    )  # fmt: skip


def inputs_version_of(connection: Connection, ctx: DestinationContext) -> str:
    """The inputs alone: what must not move while a plan is being applied."""
    editions, unit_rows, sources = _inputs(connection, ctx)
    return _version(
        ctx, editions, unit_rows, sources, None, admissions_of(connection, ctx, editions)
    )


def _world(
    records: dict[str, RecordFacts],
    series: tuple[SeriesFacts, ...],
    edition_series: dict[str, str],
) -> dict[str, object]:
    return {
        "records": sorted(
            [r.export_item_id, r.unit_id, r.state, r.rel_path, r.content_hash, r.sidecar_sha256]
            for r in records.values()
        ),
        "series": sorted(
            [
                s.export_series_id,
                s.name_raw,
                s.normalized_name,
                sorted(s.edition_ids),
                s.frozen_series,
                s.series_dir,
                s.series_key,
                s.shelf_destination_id,
            ]
            for s in series
        ),
        "links": sorted(edition_series.items()),
    }


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
            r.frozen_series if r.frozen_at is not None else None,
            r.series_dir if r.frozen_at is not None else None,
            (r.proposal_key or {}).get("series"), r.shelf_destination_id,
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
    world: dict[str, object] | None,
    admissions: dict[str, str],
) -> str:
    def unit_material(u: sa.Row[Any]) -> list[object]:
        source = sources.get(u.id)
        return [
            u.id, u.edition_id, u.number_norm, u.track, u.part, u.number_segment, u.source_order,
            source.content_hash if source and source.local_path else None,
            bool(source and source.watermarked),
        ]  # fmt: skip

    material: dict[str, object] = {
        "destination": asdict(ctx.facts()) | {"template": ctx.template.dumps()},
        "retention": ctx.retention_mode,
        "root": ctx.root_local,
        "editions": [asdict(e) for e in editions],
        "units": [unit_material(u) for u in unit_rows],
        "admissions": sorted(admissions.items()),  # a rating that moves stales the plan
    }
    if world is not None:
        material["world"] = world
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).digest()
    return digest[:16].hex()  # 128 bits, hex text: a strong ETag
