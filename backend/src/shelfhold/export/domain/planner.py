"""The planner: one pure function, shared by the dry run and the application (EF-EXP-008).

`plan_destination(request, parse)` turns what the database says (Units received, the manifest,
the series already exported, the bindings) into a `Plan`: the operations to run, what is blocked
and why, and which checks the transport asks for. It touches neither the disk nor the network;
`parse` is `plugin/parse` (already fetched by `parse-bulk`) and the loader of the application
fills the request. Collisions of series names are found here, across shelves that share a remote
library (EF-EXP-062); a watermarked file never goes to an external egress (EF-EXP-058).

The barrier of a family shelf (ADR-0027) is here too, on the one road every write takes: for a
shelf below 18+, an Edition is planned only when its admission is `allowed` (rated by an admin, not
over the ceiling, no suggested age above its rating), the destination partitions access, the Kavita
release has no known access-control flaw, the remote library is not shared with another tier and the
exported series belongs to this shelf alone. Anything the loader did not say is refused.

Idempotence (EF-EXP-007): a Unit whose record is written and whose `content_hash` and sidecar
hash are those of today yields no operation at all.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Final, Literal

from shelfhold.export.domain.bindings import is_family
from shelfhold.export.domain.export_series import EditionNaming, propose_series_name
from shelfhold.export.domain.naming import (
    DEFAULT_TEMPLATE,
    FrozenAttrs,
    NamingGuardError,
    NamingTemplate,
    Parsed,
    checked_export_path,
)

__all__ = [
    "Blocked",
    "DestinationFacts",
    "EditionFacts",
    "Plan",
    "PlanRequest",
    "PlanWarning",
    "PlannedOp",
    "RecordFacts",
    "SeriesFacts",
    "SeriesRef",
    "UnitFacts",
    "plan_destination",
]

EditionFacts = EditionNaming
TREE_CHECKS: Final = ("sentinel", "reconcile", "foreign_writer")  # only the tree declares them
PATH_TRANSPORTS: Final = frozenset({"tree", "dropbox"})
DONE_STATES: Final = frozenset({"written", "scanned", "resolved", "adopted"})
GONE_STATES: Final = frozenset({"missing", "recycled"})
SUPPORTED_TRANSPORTS: Final = PATH_TRANSPORTS | {"pull"}  # `push` arrives in M4


@dataclass(frozen=True)
class DestinationFacts:
    shelf_destination_id: str
    shelf_id: str
    transport: str = "tree"
    egress: str = "foyer"
    remote_library_id: str | None = None
    template: NamingTemplate = DEFAULT_TEMPLATE
    max_age_rating: int | None = None
    partitioning: str = "library"  # how the destination keeps accounts apart (EF-EXP-047)
    access_control_known_bad: bool = False  # Kavita release with the flaw, or none read (K4)


# The refusal of each admission verdict, as a catalog code (EF-BIB-055)
ADMISSION_CODES: Final = {
    "unrated_edition": "shelf.rating_required",
    "above_ceiling": "shelf.above_ceiling",
    "suggestion_above": "shelf.suggestion_above",
}


@dataclass(frozen=True)
class UnitFacts:
    unit_id: str
    edition_id: str
    number_norm: str
    track: str = "main"
    part: int = 1
    segment: str = ""
    kind: str = "unit"
    source_order: int = 0
    content_hash: str | None = None  # None: nothing received yet, nothing to export
    sidecar_sha256: str | None = (
        None  # the ComicInfo as it would be written, known for exported Units
    )
    watermarked: bool = False


@dataclass(frozen=True)
class RecordFacts:
    export_item_id: str
    unit_id: str
    state: str
    rel_path: str | None
    content_hash: str | None
    sidecar_sha256: str | None
    frozen: FrozenAttrs


@dataclass(frozen=True)
class SeriesFacts:
    """A series of the remote library, whichever shelf exported it."""

    export_series_id: str
    remote_library_id: str
    name_raw: str
    normalized_name: str
    work_ids: frozenset[str] = frozenset()
    edition_ids: frozenset[str] = frozenset()
    frozen_series: str | None = None  # an adopted series: the `Series` its files carry
    series_dir: str | None = None  # ... and the folder they are in (EF-EXP-052)
    series_key: str | None = None  # the catalog Series it was proposed from (EF-BIB-025)
    shelf_destination_id: str | None = None  # the shelf that made it: a series has one (EF-BIB-037)


@dataclass(frozen=True)
class PlanRequest:
    destination: DestinationFacts
    editions: tuple[EditionFacts, ...]
    units: tuple[UnitFacts, ...]
    records: tuple[RecordFacts, ...] = ()
    series: tuple[SeriesFacts, ...] = ()
    edition_series: Mapping[str, str] = field(default_factory=dict)
    # Both take a path relative to the destination root: already there / under an open intent (I9)
    occupied: Callable[[str], bool] = lambda rel: False
    covered: Callable[[str], bool] = lambda rel: False
    library_ceilings: frozenset[int | None] = frozenset()  # shelves sharing the remote library
    # edition -> `Admission` value for a shelf below 18+; an Edition missing from it is refused
    admissions: Mapping[str, str] = field(default_factory=dict)
    paused_series: frozenset[str] = frozenset()  # paused by a foreign writer (EF-EXP-043)
    foreign_names: frozenset[str] = frozenset()  # normalized names Kavita shows, from any source
    segment_policy_confirmed: bool = False


@dataclass(frozen=True)
class SeriesRef:
    """The series an operation writes into; `export_series_id` is None until it is created."""

    edition_id: str
    name_raw: str
    normalized_name: str
    export_series_id: str | None = None
    proposal_key: Mapping[str, object] | None = None
    frozen_series: str | None = None
    series_dir: str | None = None


@dataclass(frozen=True)
class PlannedOp:
    kind: Literal["write", "register"]
    unit_id: str
    work_id: str
    rel_path: str | None
    series: SeriesRef
    frozen: Mapping[str, object]
    content_hash: str
    export_item_id: str | None = None  # the item of a record left `planned` or `error`


@dataclass(frozen=True)
class Blocked:
    unit_id: str | None
    code: str
    detail: str = ""
    component: str | None = None
    suggestion: str | None = None


@dataclass(frozen=True)
class PlanWarning:
    code: str
    unit_id: str | None = None


@dataclass(frozen=True)
class Plan:
    ops: tuple[PlannedOp, ...] = ()
    blocked: tuple[Blocked, ...] = ()
    warnings: tuple[PlanWarning, ...] = ()
    checks: tuple[str, ...] = ()


def _part(text: str | int) -> int:
    return int(text) if str(text).isdigit() and int(text) >= 1 else 1


def plan_destination(request: PlanRequest, parse: Callable[[str], Parsed]) -> Plan:
    dest = request.destination
    blocked: list[Blocked] = []
    warnings: list[PlanWarning] = []
    ready = [u for u in request.units if u.content_hash is not None]
    if dest.transport not in SUPPORTED_TRANSPORTS:
        blocked = [
            Blocked(u.unit_id, "export.transport_unsupported", dest.transport) for u in ready
        ]
        return Plan(blocked=tuple(blocked))
    if dest.transport == "tree" and dest.remote_library_id is None:
        blocked = [Blocked(u.unit_id, "export.no_remote_library") for u in ready]
        return Plan(blocked=tuple(blocked), checks=TREE_CHECKS)
    family = is_family(dest.max_age_rating)
    if family:
        refusal = _family_refusal(request)
        if refusal is not None:
            return Plan(
                blocked=tuple(Blocked(u.unit_id, refusal) for u in ready), checks=TREE_CHECKS
            )
    elif len(request.library_ceilings | {dest.max_age_rating}) > 1:
        warnings.append(PlanWarning("age_tier_mix"))  # tiers mixed in one remote library
    records = {r.unit_id: r for r in request.records}
    editions = {e.edition_id: e for e in request.editions}
    known = {s.export_series_id: s for s in request.series}
    series_of = _series_resolution(request, editions, known, ready)

    ops: list[PlannedOp] = []
    seen: set[str] = set()
    for unit in ready:
        record = records.get(unit.unit_id)
        if record is not None and record.state in GONE_STATES:
            warnings.append(PlanWarning("record_gone", unit.unit_id))
            continue
        if record is not None and record.state in DONE_STATES:
            # Replacing a written file is only ever a proposal (EF-EXP-037, M2), never a plan op.
            if record.state == "adopted":
                continue  # an adopted file is kept and linked: never compared, never replaced
            if record.content_hash != unit.content_hash:
                warnings.append(PlanWarning("content_changed", unit.unit_id))
            elif unit.sidecar_sha256 not in (None, record.sidecar_sha256):
                warnings.append(PlanWarning("sidecar_changed", unit.unit_id))
            continue  # otherwise nothing changed since the last export: no operation
        if family:
            verdict = request.admissions.get(unit.edition_id, "unrated_edition")  # closed
            if verdict != "allowed":
                code = ADMISSION_CODES.get(verdict, "shelf.rating_required")
                blocked.append(Blocked(unit.unit_id, code, verdict))
                continue
        if unit.watermarked and dest.egress == "externe":
            blocked.append(Blocked(unit.unit_id, "export.watermarked_external_egress"))
            continue
        resolution = series_of(unit.edition_id)
        if isinstance(resolution, Blocked):
            blocked.append(
                Blocked(
                    unit.unit_id, resolution.code, resolution.detail, None, resolution.suggestion
                )
            )
            continue
        if (
            family
            and resolution.export_series_id is not None
            and known[resolution.export_series_id].shelf_destination_id
            not in (None, dest.shelf_destination_id)
        ):
            blocked.append(Blocked(unit.unit_id, "export.series_other_shelf"))
            continue  # one Kavita series, one shelf (EF-BIB-037)
        if resolution.export_series_id in request.paused_series:
            blocked.append(Blocked(unit.unit_id, "target.foreign_writer_detected"))
            continue
        # an adopted series is written under the name and in the folder it already has
        frozen = (
            record.frozen
            if record is not None
            else _frozen(unit, resolution.frozen_series or resolution.name_raw, dest)
        )
        if dest.transport == "pull":
            ops.append(_op("register", unit, editions, None, resolution, frozen, record))
            continue
        try:
            path = checked_export_path(
                frozen, parse, dest.template,
                segment_policy_confirmed=request.segment_policy_confirmed,
            )  # fmt: skip
        except NamingGuardError as error:
            blocked.append(
                Blocked(unit.unit_id, error.code, str(error), error.component, error.suggestion)
            )
            continue
        except ValueError as error:  # a number that is no plain decimal
            blocked.append(Blocked(unit.unit_id, "export.name_misread_number", str(error)))
            continue
        rel = path.rel_path
        if resolution.series_dir:
            rel = "/".join((resolution.series_dir, *path.directory[1:], path.filename))
        if request.covered(rel):
            continue  # under an open intent: invisible to planning (I9)
        if rel in seen or request.occupied(rel):
            blocked.append(Blocked(unit.unit_id, "export.destination_occupied", rel))
            continue
        seen.add(rel)
        ops.append(_op("write", unit, editions, rel, resolution, frozen, record))
    ops.sort(key=lambda o: (o.rel_path or "", o.unit_id))
    checks = TREE_CHECKS if dest.transport == "tree" else ()
    return Plan(tuple(ops), tuple(blocked), tuple(warnings), checks)


def _family_refusal(request: PlanRequest) -> str | None:
    """Why nothing at all may be planned for a shelf below 18+, or None."""
    dest = request.destination
    if dest.partitioning not in ("library", "user"):
        return "shelf.destination_not_partitioned"
    if dest.access_control_known_bad:
        return "target.child_exposure"
    if len(request.library_ceilings | {dest.max_age_rating}) > 1:
        return "export.age_tier_mix"  # a family library holds family shelves only
    return None


def _frozen(unit: UnitFacts, series_name: str, dest: DestinationFacts) -> FrozenAttrs:
    return FrozenAttrs(
        series=series_name, track=unit.track, number_norm=unit.number_norm, part=_part(unit.part),
        segment=unit.segment, kind=unit.kind, source_order=unit.source_order,
        template_ref=dest.template.ref,
    )  # fmt: skip


def _op(
    kind: Literal["write", "register"],
    unit: UnitFacts,
    editions: Mapping[str, EditionFacts],
    rel_path: str | None,
    series: SeriesRef,
    frozen: FrozenAttrs,
    record: RecordFacts | None,
) -> PlannedOp:
    return PlannedOp(
        kind=kind,
        unit_id=unit.unit_id,
        work_id=editions[unit.edition_id].work_id,
        rel_path=rel_path,
        series=series,
        frozen=asdict(frozen),
        content_hash=str(unit.content_hash),
        export_item_id=record.export_item_id if record is not None else None,
    )


def _series_of_same_group(
    known: Iterable[SeriesFacts], library: str | None, ref: SeriesRef
) -> SeriesFacts | None:
    """The exported series already made from the same catalog Series under this name."""
    group = (ref.proposal_key or {}).get("series")
    if not group:
        return None
    return next(
        (
            s for s in known
            if s.remote_library_id == library and s.normalized_name == ref.normalized_name
            and s.series_key == group
        ),
        None,
    )  # fmt: skip


def _series_resolution(
    request: PlanRequest,
    editions: Mapping[str, EditionFacts],
    known: Mapping[str, SeriesFacts],
    ready: list[UnitFacts],
) -> Callable[[str], SeriesRef | Blocked]:
    """Which series each Edition writes into: its link if it has one, else a proposal, refused
    when its normalized name is already used in the remote library (EF-EXP-032, EF-EXP-062)."""
    library = request.destination.remote_library_id
    siblings: list[EditionNaming] = list(editions.values())
    for existing in known.values():  # Works already exported there by any shelf share the library
        siblings += [
            EditionNaming(f"series:{existing.export_series_id}", work, "", "")
            for work in existing.work_ids
        ]
    pending = {u.edition_id for u in ready}
    resolved: dict[str, SeriesRef | Blocked] = {}
    proposals: dict[str, SeriesRef] = {}
    for edition_id in sorted(pending):
        edition = editions.get(edition_id)
        if edition is None:
            resolved[edition_id] = Blocked(None, "export.series_name_collision", "unknown edition")
            continue
        linked = request.edition_series.get(edition_id)
        if linked is not None and linked in known:
            series = known[linked]
            resolved[edition_id] = SeriesRef(
                edition_id,
                series.name_raw,
                series.normalized_name,
                series.export_series_id,
                frozen_series=series.frozen_series,
                series_dir=series.series_dir,
            )
            continue
        proposal = propose_series_name(edition, siblings)
        ref = SeriesRef(edition_id, proposal.name, proposal.normalized, None, proposal.key)
        joined = _series_of_same_group(known.values(), library, ref)
        if joined is not None:  # another album of the Series already made the exported series
            resolved[edition_id] = SeriesRef(
                edition_id, joined.name_raw, joined.normalized_name, joined.export_series_id,
                frozen_series=joined.frozen_series, series_dir=joined.series_dir,
            )  # fmt: skip
            continue
        proposals[edition_id] = ref
    owners: dict[str, set[str]] = defaultdict(set)  # who asks for a name: a Series counts once
    for edition_id, ref in proposals.items():
        group = (ref.proposal_key or {}).get("series")
        owners[ref.normalized_name].add(str(group) if group else f"edition:{edition_id}")
    in_library = {s.normalized_name for s in known.values() if s.remote_library_id == library}
    in_library |= request.foreign_names  # a series of another source bears its name too
    for edition_id, ref in proposals.items():
        if ref.normalized_name in in_library or len(owners[ref.normalized_name]) > 1:
            resolved[edition_id] = Blocked(
                None, "export.series_name_collision", ref.name_raw,
                suggestion="define an exported series name",
            )  # fmt: skip
        else:
            resolved[edition_id] = ref
    return lambda edition_id: resolved[edition_id]
