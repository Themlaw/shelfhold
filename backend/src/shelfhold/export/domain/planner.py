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

An EPUB carries no age (D1): the age of its series reaches Kavita by the API, locked (EF-EXP-040).
So an EPUB of a Work with an age to push is planned only through the age gate (`AgeGateFacts`,
read from Kavita by the export job just before planning): the first EPUB of a series goes alone,
and only while Kavita answers on a release at the floor of ENF-COMP-001; the rest of the series
waits (`export.awaiting_series_age`) until its age has been read back locked at the value wanted
now, after the scan. Anything the gate did not say keeps it closed. A shelf of book Works aims at a
Kavita Books library (D19): another type is announced, never changed.
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
    volume_number,
)
from shelfhold.sdk.age import AgeRating

__all__ = [
    "AGE_PAUSES",
    "AGE_WAIT",
    "GATE_CLOSED",
    "SERIES_AGE_STALE",
    "SERIES_SPLIT",
    "AgeGateFacts",
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
    "VolumeFacts",
    "age_rank",
    "plan_destination",
    "series_key",
]

EditionFacts = EditionNaming
TREE_CHECKS: Final = ("sentinel", "reconcile", "foreign_writer")  # only the tree declares them
PATH_TRANSPORTS: Final = frozenset({"tree", "dropbox"})
DONE_STATES: Final = frozenset({"written", "scanned", "resolved", "adopted"})
VOLUME_KINDS: Final = frozenset({"volume", "bundle"})  # the items of a range (EF-BIB-044)
VOLUME_WAITS: Final = "volume_waits"  # a warning: the policy keeps the volume for later
VOLUME_REFUSED: Final = "volume_refused"  # a warning: never, for this novel under this policy
SOURCE_MISSING: Final = "export.ranged_source_missing"
VOLUME_UNNAMED: Final = "export.volume_unnamed"
VOLUME_UNIT_EXPORTED: Final = "export.volume_unit_already_exported"
GONE_STATES: Final = frozenset({"missing", "recycled"})
SUPPORTED_TRANSPORTS: Final = PATH_TRANSPORTS | {"pull"}  # `push` arrives in M4
AGE_WAIT: Final = "export.awaiting_series_age"
EXTENSIONS: Final = {"cbz": ".cbz", "epub": ".epub"}


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
    library_type: str | None = None  # the Kavita library type the admin declared (D19)


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
    format: str = "cbz"  # "epub": the file carries no age, its series goes through the age gate


@dataclass(frozen=True)
class VolumeFacts:
    """An export item of a range: one EPUB for several Units (EF-BIB-044, `/data/ranged`).

    `label` is what the source called the volume ("Vol.3"); the number of the name is read from
    it. `closed`: the source will not add to it. `verdict` is what the volume policy says (the
    value of `ExportVerdict`: `export`, `export_tail`, `wait`, `refuse`), read by the loader.
    `content_hash` is the fingerprint of the kept file; None when it is missing.
    """

    export_item_id: str
    edition_id: str
    label: str | None
    closed: bool
    unit_ids: tuple[str, ...]
    content_hash: str | None = None
    verdict: str = "export"
    # the chapters a re-cut Unit of the volume became (`unit_coverage` of carrier `unit`): they
    # are the volume's too, and are never exported beside it
    child_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgeGateFacts:
    """What Kavita said of the series ages just before planning (EF-EXP-040).

    `open`: Kavita answered on a release at the floor (ENF-COMP-001), so a first EPUB may go.
    `verified`: the export series whose age Kavita shows locked, by Shelfhold, at the value wanted
    now. The default says nothing, so nothing goes.
    """

    open: bool = False
    verified: frozenset[str] = frozenset()
    # the lowest tier of a non-admin account that reaches the library with "include unknowns" on:
    # a series of "unknown" age is shown to it, so no first EPUB above that tier may go
    unknowns_tier: int | None = None


# `export_series.paused_reason` set by the age gate: the series takes nothing until it clears
SERIES_AGE_STALE: Final = "series_age_stale"  # Kavita shows an age below the one wanted now
SERIES_SPLIT: Final = "series_split"  # Kavita split the files of the series into two series
AGE_PAUSES: Final = frozenset({SERIES_AGE_STALE, SERIES_SPLIT})


def age_rank(age: AgeRating) -> int:
    """An age as a tier of `kavita_access.tier`: X reads as 99."""
    return 99 if age.x else age.min_age


GATE_CLOSED: Final = AgeGateFacts()


@dataclass(frozen=True)
class RecordFacts:
    """A manifest line, by export item. `unit_id` is the one Unit of a file of its own (a chapter);
    `unit_ids` are all the Units the item covers, which a volume or an adopted file has several
    of (then `unit_id` is None)."""

    export_item_id: str
    unit_id: str | None
    state: str
    rel_path: str | None
    content_hash: str | None
    sidecar_sha256: str | None
    frozen: FrozenAttrs
    kind: str = "unit"
    unit_ids: tuple[str, ...] = ()


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
    volumes: tuple[VolumeFacts, ...] = ()  # the items of a range: planned like files of their own
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
    holds_books: bool = False  # the shelf takes book Works (`novel`, `light_novel`, `ebook`)
    age_wanted: frozenset[str] = frozenset()  # Editions whose series has an age to push
    age_seeded: frozenset[str] = frozenset()  # export series that already hold an exported file
    age_unknown_exposed: frozenset[str] = frozenset()  # above `unknowns_tier`: no first EPUB
    age_gate: AgeGateFacts = GATE_CLOSED


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
    unit_id: str | None  # None for a volume: its `export_item_id` says what it is
    work_id: str
    rel_path: str | None
    series: SeriesRef
    frozen: Mapping[str, object]
    content_hash: str
    # the item of a record left `planned` or `error`, and always the item of a volume
    export_item_id: str | None = None


@dataclass(frozen=True)
class Blocked:
    unit_id: str | None
    code: str
    detail: str = ""
    component: str | None = None
    suggestion: str | None = None
    export_item_id: str | None = None  # a volume has no Unit to name


@dataclass(frozen=True)
class PlanWarning:
    code: str
    unit_id: str | None = None
    export_item_id: str | None = None


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
    volumes = sorted(request.volumes, key=lambda v: v.export_item_id)
    if dest.transport not in SUPPORTED_TRANSPORTS:
        blocked = [
            Blocked(u.unit_id, "export.transport_unsupported", dest.transport) for u in ready
        ]
        blocked += _volumes_blocked(volumes, "export.transport_unsupported", dest.transport)
        return Plan(blocked=tuple(blocked))
    if dest.transport == "tree" and dest.remote_library_id is None:
        blocked = [Blocked(u.unit_id, "export.no_remote_library") for u in ready]
        blocked += _volumes_blocked(volumes, "export.no_remote_library")
        return Plan(blocked=tuple(blocked), checks=TREE_CHECKS)
    family = is_family(dest.max_age_rating)
    if family:
        refusal = _family_refusal(request)
        if refusal is not None:
            blocked = [Blocked(u.unit_id, refusal) for u in ready]
            blocked += _volumes_blocked(volumes, refusal)
            return Plan(blocked=tuple(blocked), checks=TREE_CHECKS)
    elif len(request.library_ceilings | {dest.max_age_rating}) > 1:
        warnings.append(PlanWarning("age_tier_mix"))  # tiers mixed in one remote library
    if request.holds_books and (dest.library_type or "").casefold() != "book":
        warnings.append(PlanWarning("target.library_type_mismatch"))  # D19: announced, not fixed
    by_item = {r.export_item_id: r for r in request.records}
    by_unit: dict[str, RecordFacts] = {}
    for line in request.records:
        if line.kind not in VOLUME_KINDS:
            for covered_unit in (*([line.unit_id] if line.unit_id else []), *line.unit_ids):
                by_unit.setdefault(covered_unit, line)
    # A Unit a volume carries is the volume's, never also a chapter of its own (one file per Unit)
    in_volume = {uid for v in volumes for uid in (*v.unit_ids, *v.child_ids)}
    in_volume |= {uid for r in request.records if r.kind in VOLUME_KINDS for uid in r.unit_ids}
    editions = {e.edition_id: e for e in request.editions}
    known = {s.export_series_id: s for s in request.series}
    pending = {u.edition_id for u in ready if u.unit_id not in in_volume}
    pending |= {v.edition_id for v in volumes}
    series_of = _series_resolution(request, editions, known, pending)

    ops: list[PlannedOp] = []
    seen: set[str] = set()
    seeds: set[str] = set()  # the series whose first EPUB this plan already sends
    for unit in ready:
        if unit.unit_id in in_volume:
            continue
        record = by_unit.get(unit.unit_id)
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
        if not _age_gate_lets(request, unit.edition_id, unit.format == "epub", resolution, seeds):
            blocked.append(Blocked(unit.unit_id, AGE_WAIT))
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
                frozen, parse, dest.template, EXTENSIONS.get(unit.format, ".cbz"),
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
    taken = {
        uid for r in request.records if r.kind in VOLUME_KINDS and r.state in DONE_STATES
        for uid in r.unit_ids
    }  # fmt: skip
    for volume in volumes:
        _plan_volume(
            request, volume, by_item.get(volume.export_item_id), by_unit,
            _Shared(
                parse, editions, known, series_of, family, seen, seeds, taken, ops, blocked,
                warnings,
            ),
        )  # fmt: skip
    ops.sort(key=lambda o: (o.rel_path or "", o.unit_id or "", o.export_item_id or ""))
    checks = TREE_CHECKS if dest.transport == "tree" else ()
    return Plan(tuple(ops), tuple(blocked), tuple(warnings), checks)


def _volumes_blocked(volumes: Iterable[VolumeFacts], code: str, detail: str = "") -> list[Blocked]:
    return [Blocked(None, code, detail, export_item_id=v.export_item_id) for v in volumes]


@dataclass
class _Shared:
    """What the loop over the Units leaves for the loop over the volumes: one plan, one set of
    names taken, one set of series whose first EPUB has gone."""

    parse: Callable[[str], Parsed]
    editions: Mapping[str, EditionFacts]
    known: Mapping[str, SeriesFacts]
    resolve_series: Callable[[str], "SeriesRef | Blocked"]
    family: bool
    seen: set[str]
    seeds: set[str]
    units_taken: set[str]  # the Units of a volume already left, or planned, in another volume
    ops: list[PlannedOp]
    blocked: list[Blocked]
    warnings: list[PlanWarning]


def _plan_volume(
    request: PlanRequest,
    volume: VolumeFacts,
    record: RecordFacts | None,
    by_unit: Mapping[str, RecordFacts],
    shared: _Shared,
) -> None:
    """One export item of a range, by the roads of a file of its own: its Edition admitted on a
    family shelf, its series free of a foreign writer and through the age gate (it is an EPUB),
    its name read by `plugin/parse`. Whether it goes at all is the policy's verdict (L26): a volume
    still open under `closed_only` waits; the open tail already served is planned again, at the
    path it has, only when the policy rolls it and the kept file is no longer the one served."""
    dest, blocked = request.destination, shared.blocked
    item = volume.export_item_id

    def stop(code: str, detail: str = "") -> None:
        blocked.append(Blocked(None, code, detail, export_item_id=item))

    replaced = False
    if record is not None and record.state in GONE_STATES:
        shared.warnings.append(PlanWarning("record_gone", export_item_id=item))
        return
    if record is not None and record.state in DONE_STATES:
        if record.state == "adopted" or volume.closed or dest.transport == "pull":
            return  # a closed volume is frozen: never compared, never replaced
        if volume.verdict != "export_tail":
            return  # nothing rolls the tail under this policy: the served copy stays
        if volume.content_hash is None or volume.content_hash == record.content_hash:
            return
        replaced = True
    else:
        if volume.verdict in ("wait", "refuse"):
            code = VOLUME_WAITS if volume.verdict == "wait" else VOLUME_REFUSED
            shared.warnings.append(PlanWarning(code, export_item_id=item))
            return
        if volume.content_hash is None:
            stop(SOURCE_MISSING)
            return
        chapter_states = DONE_STATES | {"planned"}
        taken = [
            u
            for u in volume.unit_ids
            if u in shared.units_taken or (u in by_unit and by_unit[u].state in chapter_states)
        ]
        if taken:  # a Unit already left as a chapter: never twice
            stop(VOLUME_UNIT_EXPORTED, taken[0])
            return
    if shared.family:
        verdict = request.admissions.get(volume.edition_id, "unrated_edition")  # closed
        if verdict != "allowed":
            stop(ADMISSION_CODES.get(verdict, "shelf.rating_required"), verdict)
            return
    resolution = shared.resolve_series(volume.edition_id)
    if isinstance(resolution, Blocked):
        stop(resolution.code, resolution.detail)
        return
    if (
        shared.family
        and resolution.export_series_id is not None
        and shared.known[resolution.export_series_id].shelf_destination_id
        not in (None, dest.shelf_destination_id)
    ):
        stop("export.series_other_shelf")  # one Kavita series, one shelf (EF-BIB-037)
        return
    if resolution.export_series_id in request.paused_series:
        stop("target.foreign_writer_detected")
        return
    if not _age_gate_lets(request, volume.edition_id, True, resolution, shared.seeds):
        stop(AGE_WAIT)
        return
    # The name comes from the label the source gave and from the series of this destination, never
    # from what the item keeps: the item is shared by every destination (I22 holds per line)
    number = volume_number(volume.label)
    if number is None:
        stop(VOLUME_UNNAMED)
        return
    frozen = FrozenAttrs(
        series=resolution.frozen_series or resolution.name_raw, track="main",
        number_norm=number, kind="volume", template_ref=dest.template.ref,
    )  # fmt: skip
    if replaced and record is not None:  # the same path, the same name: the upgrade's business
        shared.ops.append(
            _volume_op("write", volume, shared.editions, record.rel_path, resolution, frozen)
        )
        return
    if dest.transport == "pull":
        shared.ops.append(_volume_op("register", volume, shared.editions, None, resolution, frozen))
        return
    try:
        path = checked_export_path(
            frozen, shared.parse, dest.template, ".epub",
            segment_policy_confirmed=request.segment_policy_confirmed,
        )  # fmt: skip
    except NamingGuardError as error:
        blocked.append(
            Blocked(None, error.code, str(error), error.component, error.suggestion, item)
        )
        return
    except ValueError as error:
        stop("export.name_misread_number", str(error))
        return
    rel = path.rel_path
    if resolution.series_dir:
        rel = "/".join((resolution.series_dir, *path.directory[1:], path.filename))
    if request.covered(rel):
        return  # under an open intent: invisible to planning (I9)
    if rel in shared.seen or request.occupied(rel):
        stop("export.destination_occupied", rel)
        return
    shared.seen.add(rel)
    shared.units_taken.update(volume.unit_ids)
    shared.ops.append(_volume_op("write", volume, shared.editions, rel, resolution, frozen))


def _volume_op(
    kind: Literal["write", "register"],
    volume: VolumeFacts,
    editions: Mapping[str, EditionFacts],
    rel_path: str | None,
    series: SeriesRef,
    frozen: FrozenAttrs,
) -> PlannedOp:
    return PlannedOp(
        kind=kind, unit_id=None, work_id=editions[volume.edition_id].work_id, rel_path=rel_path,
        series=series, frozen=asdict(frozen), content_hash=str(volume.content_hash),
        export_item_id=volume.export_item_id,
    )  # fmt: skip


def series_key(ref: SeriesRef) -> str:
    """The series an operation writes into, before it exists: its id, else its proposed name."""
    return ref.export_series_id or f"new:{ref.normalized_name}"


def _age_gate_lets(
    request: PlanRequest, edition_id: str, is_epub: bool, ref: SeriesRef, seeds: set[str]
) -> bool:
    """May this file (a Unit's, or a volume's: always an EPUB) be planned as far as the age of its
    series goes (EF-EXP-040)?

    Only an EPUB of an Edition with an age to push is gated (a CBZ carries its age in ComicInfo;
    an unrated Edition has none to push, and a shelf below 18+ already refuses it). A `pull`
    destination has no Kavita series. A series whose age is verified takes everything; a series
    that holds no file yet takes one first EPUB, while Kavita answers; any other waits.
    """
    if not is_epub or edition_id not in request.age_wanted:
        return True
    if request.destination.transport == "pull":
        return True
    gate = request.age_gate
    if ref.export_series_id is not None and ref.export_series_id in gate.verified:
        return True
    key = series_key(ref)
    if not gate.open or key in seeds:
        return False
    if ref.export_series_id is not None and ref.export_series_id in request.age_seeded:
        return False  # it holds files whose age is not read back locked yet
    if edition_id in request.age_unknown_exposed:
        return False  # "unknown" until the push, and an account restricted below sees unknowns
    seeds.add(key)
    return True


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
    pending: set[str],
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
