"""Ingest one release: Unit (the place), Variant (the exemplar), revision (EF-BIB-034, EF-BIB-050).

Nothing here touches `content`: before M2 a republication is recorded without replacing the
exported file (no upgrade before EF-EXP-016), and a revision never moves a Variant.
"""

from dataclasses import replace

from shelfhold.catalog.application.dto import IncomingUnit, IngestResult
from shelfhold.catalog.application.ports import StoredVariant, TeamStore, UnitStore
from shelfhold.catalog.application.teams import check_binding, team_of_checked_binding
from shelfhold.catalog.domain.identity import sort_title
from shelfhold.catalog.domain.numbering import NumberError, UnitNumber, parse_number
from shelfhold.catalog.domain.placement import (
    NUMBERING_CONFLICT,
    TITLE_MATCH,
    IngestOutcome,
    decide_new_variant,
    place_of,
)


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


def ingest_unit(units: UnitStore, teams: TeamStore, item: IncomingUnit) -> IngestResult:
    check_binding(teams, item.binding)
    existing = units.find_variant(item.binding.id, item.remote_unit_id)
    if existing is not None:  # same (binding, remote id): the same Variant, whatever it now says
        suggestion_id = _reappear(units, existing)
        if not units.revision_changed(existing, item):
            return IngestResult(
                IngestOutcome.UNCHANGED,
                existing.unit_id,
                existing.id,
                existing.team_id,
                existing.revision,
                suggestion_id=suggestion_id,
            )
        revision = units.add_revision(existing, item)
        return IngestResult(
            IngestOutcome.REVISION,
            existing.unit_id,
            existing.id,
            existing.team_id,
            revision,
            suggestion_id=suggestion_id,
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
