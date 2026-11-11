"""Ingest one release: Unit (the place), Variant (the exemplar), revision (EF-BIB-034, EF-BIB-050).

Nothing here touches `content`: a republication is recorded without replacing the exported file
and a revision never moves a Variant. Every revision waits in `revision_pending` (EF-BIB-043); one
that counts under half the pages of the stored version, or that the source gives as `stubbed`,
is quarantined on top and the admin is warned (EF-BIB-024). A Unit a source removes keeps its
local copy (EF-BIB-009).
"""

from dataclasses import replace

from shelfhold.catalog.application.dto import IncomingUnit, IngestResult
from shelfhold.catalog.application.ports import StoredVariant, TeamStore, UnitStore
from shelfhold.catalog.application.teams import check_binding, team_of_checked_binding
from shelfhold.catalog.domain.availability import normalize_availability
from shelfhold.catalog.domain.identity import sort_title
from shelfhold.catalog.domain.numbering import NumberError, UnitNumber, parse_number
from shelfhold.catalog.domain.placement import (
    NUMBERING_CONFLICT,
    TITLE_MATCH,
    IngestOutcome,
    decide_new_variant,
    place_of,
)
from shelfhold.catalog.domain.revisions import DEFAULT_MIN_PAGE_RATIO, suspect_reason


def _number(item: IncomingUnit) -> UnitNumber:
    try:
        return parse_number(item.raw_number, source_order=item.source_order)
    except NumberError:  # negative or out of bounds: unreadable, so unnumbered (EF-BIB-034)
        return replace(
            parse_number(None, source_order=item.source_order), raw=item.raw_number or ""
        )


def _reappear(units: UnitStore, variant: StoredVariant) -> str | None:
    """A removed Variant seen again lives again; if another live one took its place meanwhile
    (same binding, same Team), that is a numbering conflict, decided like any other (EF-BIB-050).
    """
    facts = units.facts_at(variant.unit_id)
    me = next((f for f in facts if f.id == variant.id), None)
    if me is None or not me.removed:
        return None
    units.reappear(variant.id)
    others = [f for f in facts if f.id != variant.id]
    decision = decide_new_variant(binding_id=me.binding_id, team_id=me.team_id, at_place=others)
    if decision.outcome is IngestOutcome.NUMBERING_CONFLICT and decision.other_id:
        return units.open_suggestion(decision.other_id, variant.id, NUMBERING_CONFLICT)
    return None


# The one field of a revision the Kavita API can take without a file being rewritten.
API_FIELDS = frozenset({"summary"})


def _nature(
    units: UnitStore, existing: StoredVariant, item: IncomingUnit
) -> tuple[str, str | None, str | None]:
    """`metadata_only` when the file did not change and the revision carries one field the API
    takes; anything else is a `rewrite`, which only a confirmed batch can apply."""
    _hint, last_hash, last_pages = units.last_healthy_revision(existing)
    same_file = item.content_hash in (None, last_hash) and item.page_count in (None, last_pages)
    wanted = [(f, v) for f, v in item.metadata if f in API_FIELDS]
    if same_file and len(wanted) == 1 and len(item.metadata) == 1:
        return "metadata_only", wanted[0][0], wanted[0][1]
    return "rewrite", None, None


def ingest_unit(
    units: UnitStore,
    teams: TeamStore,
    item: IncomingUnit,
    *,
    min_page_ratio: float = DEFAULT_MIN_PAGE_RATIO,
) -> IngestResult:
    check_binding(teams, item.binding)
    existing = units.find_variant(item.binding.id, item.remote_unit_id)
    availability = None if item.availability is None else normalize_availability(item.availability)
    if existing is not None:  # same (binding, remote id): the same Variant, whatever it now says
        removed_now = availability is not None and availability.value == "removed"
        suggestion_id = None if removed_now else _reappear(units, existing)
        if availability is not None:
            units.apply_availability(existing.unit_id, existing.id, availability, item)
        if not units.revision_changed(existing, item):
            return IngestResult(
                IngestOutcome.UNCHANGED,
                existing.unit_id,
                existing.id,
                existing.team_id,
                existing.revision,
                suggestion_id=suggestion_id,
            )
        suspect = suspect_reason(
            stored_pages=units.stored_pages(existing),
            new_pages=item.page_count,
            availability="free" if availability is None else availability.value,
            min_ratio=min_page_ratio,
        )
        carried = units.latest_quarantine(existing)
        if suspect is None and carried and item.page_count is None:
            suspect = carried  # nothing proves this one is whole: the quarantine stays (EF-BIB-024)
        nature = ("rewrite", None, None) if suspect else _nature(units, existing, item)
        revision = units.add_revision(existing, item, quarantine=suspect)
        units.queue_revision(existing, revision, nature=nature[0], field=nature[1], value=nature[2])
        return IngestResult(
            IngestOutcome.REVISION,
            existing.unit_id,
            existing.id,
            existing.team_id,
            revision,
            suggestion_id=suggestion_id,
            suspect=suspect,
        )

    edition_id = item.binding.edition_id
    number = _number(item)
    place = place_of(number)
    team_id = (
        team_of_checked_binding(teams, item.binding, item.team_label, item.seen_at).id
        if item.team_label and item.team_label.strip()
        else None
    )
    unit_id = units.find_unit(edition_id, place)
    unit_created = unit_id is None
    if unit_id is None:
        unit_id = units.insert_unit(edition_id, number, place, item)
    decision = decide_new_variant(
        binding_id=item.binding.id, team_id=team_id, at_place=units.facts_at(unit_id)
    )
    supersedes = decision.other_id if decision.outcome is IngestOutcome.SUPERSEDING else None
    variant_id = units.insert_variant(unit_id, team_id, supersedes, item)
    if availability is not None:
        units.apply_availability(unit_id, variant_id, availability, item)
    suggestion_id = None
    if decision.outcome is IngestOutcome.NUMBERING_CONFLICT and decision.other_id:
        suggestion_id = units.open_suggestion(decision.other_id, variant_id, NUMBERING_CONFLICT)
    elif place.unnumbered and item.title and unit_created:
        # No number to place it by: if its title is that of an existing Unit, propose it (never
        # attach by itself, EF-BIB-034).
        match = units.titled_variant(edition_id, sort_title(item.title), unit_id)
        if match is not None:
            suggestion_id = units.open_suggestion(match, variant_id, TITLE_MATCH)
    return IngestResult(
        decision.outcome, unit_id, variant_id, team_id, 1, unit_created, supersedes, suggestion_id
    )


# -- a file a source hands over whole (`FileAsset`, EF-SRC-015) ------------------------------------

# What a source may announce for a passthrough file. The announcement only decides whether the
# file is taken at all: what it is, and so where it is filed, is read from its bytes by the
# receiver (`sniff_kind`), never from this label or from a file name (EF-BIB-051).
FILE_MIMES: dict[str, str] = {
    "application/epub+zip": "epub",
    "application/vnd.comicbook+zip": "cbz",
    "application/x-cbz": "cbz",
    "application/zip": "cbz",
}


class UnsupportedFileError(ValueError):
    """The source announced a file this release does not take (a PDF, audio, anything else)."""


def announced_kind(mime: str) -> str:
    """ "epub" or "cbz": what a source says of a file it hands over whole. Only a gate: the bytes
    are what the receiver reads, so the label can neither route a file nor excuse one."""
    announced = FILE_MIMES.get(mime.split(";")[0].strip().lower())
    if announced is None:
        raise UnsupportedFileError("the source announced a file type that is not taken")
    return announced
