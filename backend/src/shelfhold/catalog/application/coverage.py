"""What an EPUB range file really carries: bounds from its table of contents, and re-cut chapters.

Two jobs, both about `unit_coverage` (EF-BIB-044, EF-LEC-014):

**Bounds.** A volume delivered as one EPUB covers chapters. Each Unit's bounds in the file are the
spine items of its chapters; when the source gives none they are read from the table of contents
(`bounds_from_toc`). `check_coverage` then compares what is declared with what the table lists and
reports what would make the file look complete when it is not: a chapter of the table that no
Unit covers (a hole, or an entry that points at nothing), two Units that share spine items (an
overlap), a chapter listed or declared twice, a bound that points at nothing.
`CoverageReport.complete` is true only when none of these is found: a file is never presented as
complete on a coverage that was not verified.

**Re-cut chapters.** When the source cuts a chapter again (10 becomes 10.1 and 10.2), the old Unit
stays covered by its file and the new ones are *proposed* as covered by it
(`carrier_type = unit`): `propose_resplit` computes a proposal, `apply_resplit` writes it only with
the confirmation token the proposal carries. Nothing is ever removed, and the age of the series
is not touched here: the new Units are exported through the same age barrier as any Unit (L09).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import catalog, export

__all__ = [
    "Bound",
    "ConfirmationRequiredError",
    "CoverageMismatchError",
    "CoverageReport",
    "ResplitError",
    "ResplitProposal",
    "apply_resplit",
    "bounds_from_toc",
    "check_coverage",
    "propose_resplit",
    "stored_bounds",
    "verify_item",
]

MAX_PROPOSED: Final = 200  # children of one re-cut chapter: a typo must not fill the table


class TocChapter(Protocol):
    """A chapter of a table of contents (`infrastructure.packaging.epub.toc.TocChapter`)."""

    @property
    def title(self) -> str: ...
    @property
    def target(self) -> str: ...
    @property
    def spine_index(self) -> int: ...


class Toc(Protocol):
    @property
    def chapters(self) -> Sequence[TocChapter]: ...
    @property
    def spine(self) -> Sequence[str]: ...
    @property
    def unresolved(self) -> Sequence[str]: ...
    @property
    def spine_hashes(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class Bound:
    """The bounds of one Unit in the file: archive paths (or `path#fragment`), `None` = unknown."""

    unit_id: str
    start: str | None
    end: str | None


@dataclass(frozen=True)
class CoverageReport:
    gaps: tuple[str, ...] = ()  # titles of table entries no Unit covers
    overlaps: tuple[tuple[str, str], ...] = ()  # pairs of Units that share spine items
    duplicates: tuple[str, ...] = ()  # a Unit declared twice, a chapter listed twice
    unresolved: tuple[str, ...] = ()  # Units whose bounds point at no spine item (or back to front)
    unbounded: tuple[str, ...] = ()  # Units with no start: nothing to verify against
    empty: tuple[str, ...] = ()  # Units whose bounds hold no chapter of the table
    disordered: tuple[str, ...] = ()  # entries or Units that go back in the reading order
    copies: tuple[str, ...] = ()  # chapters whose file has the content of another chapter
    mislabeled: tuple[str, ...] = ()  # titles whose numbers do not follow the Units' numbers
    chapters: int = 0  # entries of the table that were checked

    @property
    def complete(self) -> bool:
        return self.chapters > 0 and not (
            self.gaps or self.overlaps or self.duplicates or self.unresolved or self.unbounded
            or self.empty or self.disordered or self.copies or self.mislabeled
        )  # fmt: skip

    @property
    def problems(self) -> tuple[str, ...]:
        names = (
            "gaps", "overlaps", "duplicates", "unresolved", "unbounded", "empty", "disordered",
            "copies", "mislabeled",
        )  # fmt: skip
        found = [n for n in names if getattr(self, n)]
        return tuple(found) if self.chapters else ("no_chapters", *found)


class CoverageMismatchError(ValueError):
    """The table lists a number of chapters other than the Units of the file."""


def _path(locator: str) -> str:
    return locator.split("#", 1)[0]


_NUMBER: Final = re.compile(r"\d+(?:\.\d+)?")


def _title_number(title: str) -> Decimal | None:
    found = _NUMBER.findall(title)
    return Decimal(found[-1]) if found else None


def check_coverage(
    toc: Toc, bounds: Sequence[Bound], numbers: Mapping[str, Decimal | None] | None = None
) -> CoverageReport:
    """Compare the Units a file is declared to carry (`bounds`, in file order) with the chapters
    its table lists. `numbers` (unit id -> number) lets the titles of the table be compared
    with the Units: they must follow the same numbering, whatever its offset."""
    place: dict[str, int] = {}
    for index, path in enumerate(toc.spine):
        place.setdefault(path, index)
    seen_targets: set[str] = set()
    dup_chapters: list[str] = []
    chapters: list[TocChapter] = []
    for chapter in toc.chapters:
        if chapter.target in seen_targets:
            dup_chapters.append(chapter.title or chapter.target)
            continue
        seen_targets.add(chapter.target)
        chapters.append(chapter)
    declared: set[str] = set()
    dup_units: list[str] = []
    unresolved: list[str] = []
    unbounded: list[str] = []
    spans: list[tuple[int, int, str]] = []
    placed: list[tuple[int, int, str]] = []  # the same, in the order given (positions)
    for bound in bounds:
        if bound.unit_id in declared:
            dup_units.append(bound.unit_id)
            continue
        declared.add(bound.unit_id)
        if bound.start is None:
            unbounded.append(bound.unit_id)
            continue
        first = place.get(_path(bound.start))
        last = first if bound.end is None else place.get(_path(bound.end))
        if first is None or last is None or last < first:
            unresolved.append(bound.unit_id)
            continue
        spans.append((first, last, bound.unit_id))
        placed.append((first, last, bound.unit_id))
    spans.sort()
    overlaps: list[tuple[str, str]] = []
    reach, holder = -1, ""
    for first, last, unit_id in spans:
        if first <= reach:
            overlaps.append((holder, unit_id))
        if last > reach:
            reach, holder = last, unit_id
    gaps = [
        c.title or c.target
        for c in chapters
        if not any(first <= c.spine_index <= last for first, last, _ in spans)
    ]
    # an entry that points at no item of the file is a chapter the file does not hold
    gaps.extend(f"{href} (not in the file)" for href in toc.unresolved)
    empty = [
        unit_id
        for first, last, unit_id in spans
        if not any(first <= c.spine_index <= last for c in chapters)
    ]
    disordered = [
        later.title or later.target
        for earlier, later in pairwise(chapters)
        if later.spine_index < earlier.spine_index  # the table must follow the reading order
    ]
    disordered.extend(
        later[2]
        for earlier, later in pairwise(placed)
        if later[0] < earlier[0]  # the Units must follow it too, in the order of their positions
    )
    copies: list[str] = []
    if len(toc.spine_hashes) == len(toc.spine):
        owner: dict[str, str] = {}
        for chapter in chapters:
            digest = toc.spine_hashes[chapter.spine_index]
            if not digest:
                continue
            if digest in owner and owner[digest] != chapter.path:
                copies.append(chapter.title or chapter.target)
            owner.setdefault(digest, chapter.path)
    mislabeled: list[str] = []
    if numbers is not None and len(chapters) == len(bounds):
        pairs = [
            (_title_number(c.title), numbers.get(b.unit_id))
            for c, b in zip(chapters, bounds, strict=True)
        ]
        if all(t is not None and u is not None for t, u in pairs):
            offsets = {u - t for t, u in pairs if t is not None and u is not None}
            if len(offsets) > 1:
                mislabeled = [c.title for c in chapters]
    return CoverageReport(
        gaps=tuple(gaps), overlaps=tuple(overlaps), duplicates=(*dup_units, *dup_chapters),
        unresolved=tuple(unresolved), unbounded=tuple(unbounded), empty=tuple(empty),
        disordered=tuple(disordered), copies=tuple(copies), mislabeled=tuple(mislabeled),
        chapters=len(chapters),
    )  # fmt: skip


def bounds_from_toc(toc: Toc, unit_ids: Sequence[str]) -> list[Bound]:
    """The bounds of the Units of a file, read from its table of contents: the n-th chapter is the
    n-th Unit, from its spine item to the item before the next chapter's. Refused when the table
    and the Units do not count the same chapters: nothing is guessed."""
    unique: list[TocChapter] = []
    for chapter in toc.chapters:
        if all(chapter.target != other.target for other in unique):
            unique.append(chapter)
    if len(unique) != len(unit_ids):
        raise CoverageMismatchError(
            f"the table lists {len(unique)} chapters, the file is declared to carry "
            f"{len(unit_ids)} Units"
        )
    found: list[Bound] = []
    for position, (chapter, unit_id) in enumerate(zip(unique, unit_ids, strict=True)):
        following = unique[position + 1].spine_index if position + 1 < len(unique) else None
        stop = (
            chapter.spine_index
            if following is None or following <= chapter.spine_index
            else following - 1
        )
        found.append(Bound(unit_id, chapter.target, toc.spine[stop]))
    return found


def _locator(value: Any) -> str | None:
    """A bound kept as a path, or as `{"href": path}`; anything else is no bound."""
    if isinstance(value, dict):
        value = value.get("href")
    return value if isinstance(value, str) and value else None


def stored_bounds(connection: Connection, item_id: str) -> list[Bound]:
    """The bounds kept in `unit_coverage` for an export item, in file order."""
    cov = export.unit_coverage
    rows = connection.execute(
        sa.select(cov.c.unit_id, cov.c.start_locator, cov.c.end_locator)
        .where(cov.c.carrier_type == "export_item", cov.c.carrier_id == item_id)
        .order_by(cov.c.position)
    )
    return [Bound(r.unit_id, _locator(r.start_locator), _locator(r.end_locator)) for r in rows]


def verify_item(connection: Connection, item_id: str, toc: Toc) -> CoverageReport:
    """The coverage of an export item against the table of its file. Bounds the source did not
    give are read from the table (and kept, origin unchanged) only when the counts agree; when
    they do not, the report says so and the file is not complete."""
    bounds = stored_bounds(connection, item_id)
    if bounds and all(b.start is None for b in bounds):
        try:
            bounds = bounds_from_toc(toc, [b.unit_id for b in bounds])
        except CoverageMismatchError:
            return CoverageReport(
                unbounded=tuple(b.unit_id for b in bounds), chapters=len(toc.chapters)
            )
    unit = catalog.unit
    numbers = {
        r.id: None if r.number_sort is None else Decimal(str(r.number_sort))
        for r in connection.execute(
            sa.select(unit.c.id, unit.c.number_sort).where(
                unit.c.id.in_([b.unit_id for b in bounds])
            )
        )
    }
    return check_coverage(toc, bounds, numbers)


# -- re-cut chapters -----------------------------------------------------------------------------


class ResplitError(ValueError):
    """The proposal is not one (or no longer holds): nothing is written."""


class ConfirmationRequiredError(PermissionError):
    """A proposal is applied only with its token, which an admin read and confirmed."""


_KEY: Final = secrets.token_bytes(32)  # per process: a token cannot be computed by a caller


def _sign(*parts: object) -> str:
    document = json.dumps(parts, separators=(",", ":"), default=str)
    return hmac.new(_KEY, document.encode(), hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True)
class ResplitProposal:
    edition_id: str
    parent_unit_id: str
    child_unit_ids: tuple[str, ...]
    snapshot: str  # the coverage of the parent and the children when it was proposed
    nonce: str
    token: str  # what the admin confirms: it binds the chapters, the snapshot and the nonce


def _snapshot(connection: Connection, parent: str, children: Sequence[str]) -> str:
    cov = export.unit_coverage
    rows = connection.execute(
        sa.select(cov.c.carrier_type, cov.c.carrier_id, cov.c.unit_id, cov.c.position).where(
            sa.or_(
                cov.c.unit_id.in_([parent, *children]),
                sa.and_(cov.c.carrier_type == "unit", cov.c.carrier_id == parent),
            )
        )
    ).all()
    document = json.dumps(sorted(tuple(r) for r in rows), separators=(",", ":"))
    return hashlib.sha256(document.encode()).hexdigest()


def _sub_number(parent: str, child: str) -> bool:
    """`10.1` is a sub-number of `10`; nothing else is."""
    return child.startswith(parent + ".") and child[len(parent) + 1 :].isdigit()


def propose_resplit(
    connection: Connection, parent_unit_id: str, child_unit_ids: Sequence[str]
) -> ResplitProposal:
    """The old Unit `parent_unit_id` is cut again into `child_unit_ids` (10 into 10.1 and 10.2):
    the proposal says the children are covered by the parent. Only the sub-numbers of the parent,
    not yet carried by any file, can be its children. Nothing is written."""
    children = tuple(dict.fromkeys(child_unit_ids))
    if not children or len(children) > MAX_PROPOSED:
        raise ResplitError(f"a chapter is cut into 1 to {MAX_PROPOSED} chapters")
    if parent_unit_id in children:
        raise ResplitError("a chapter cannot cover itself")
    unit = catalog.unit
    rows = {
        r.id: r
        for r in connection.execute(
            sa.select(unit.c.id, unit.c.edition_id, unit.c.number_norm, unit.c.track).where(
                unit.c.id.in_([parent_unit_id, *children])
            )
        )
    }
    if parent_unit_id not in rows or any(c not in rows for c in children):
        raise ResplitError("an unknown chapter")
    parent = rows[parent_unit_id]
    if any(rows[c].edition_id != parent.edition_id for c in children):
        raise ResplitError("the chapters belong to different Editions")
    if any(
        rows[c].track != parent.track or not _sub_number(parent.number_norm, rows[c].number_norm)
        for c in children
    ):
        raise ResplitError("a re-cut chapter is cut into sub-numbers of its own (10 into 10.1)")
    cov = export.unit_coverage
    carried = connection.execute(
        sa.select(cov.c.unit_id)
        .where(cov.c.carrier_type == "export_item", cov.c.unit_id.in_(children))
        .limit(1)
    ).first()
    if carried is not None:
        raise ResplitError("a chapter is already carried by a file")
    snapshot = _snapshot(connection, parent_unit_id, children)
    nonce = secrets.token_hex(8)
    token = _sign(parent.edition_id, parent_unit_id, children, snapshot, nonce)
    return ResplitProposal(str(parent.edition_id), parent_unit_id, children, snapshot, nonce, token)


def apply_resplit(
    connection: Connection, proposal: ResplitProposal, *, confirmation: str | None
) -> int:
    """Write the coverage `carrier_type = unit` of a proposal the admin confirmed (the token it
    carries). The old Unit and its coverage by its file stay as they are. Returns the number of
    children newly covered. A proposal built by hand, or made stale by a change of the coverage
    since, is refused."""
    genuine = proposal.token == _sign(
        proposal.edition_id, proposal.parent_unit_id, proposal.child_unit_ids,
        proposal.snapshot, proposal.nonce,
    )  # fmt: skip
    if confirmation is None or not genuine or not hmac.compare_digest(confirmation, proposal.token):
        raise ConfirmationRequiredError("a re-cut chapter is applied only once confirmed")
    cov = export.unit_coverage
    held = set(
        connection.execute(
            sa.select(cov.c.unit_id).where(
                cov.c.carrier_type == "unit", cov.c.carrier_id == proposal.parent_unit_id
            )
        ).scalars()
    )
    if held == set(proposal.child_unit_ids):  # already applied: nothing changes
        return 0
    try:
        again = propose_resplit(connection, proposal.parent_unit_id, proposal.child_unit_ids)
    except ResplitError:
        raise ResplitError("the proposal does not hold any more") from None
    if again.snapshot != proposal.snapshot or again.edition_id != proposal.edition_id:
        raise ResplitError("the proposal does not hold any more")
    top = connection.execute(
        sa.select(sa.func.max(cov.c.position)).where(
            cov.c.carrier_type == "unit", cov.c.carrier_id == proposal.parent_unit_id
        )
    ).scalar()
    position = 0 if top is None else int(top) + 1
    added = 0
    for child in proposal.child_unit_ids:
        if child in held:
            continue
        connection.execute(
            sa.insert(cov).values(
                carrier_type="unit", carrier_id=proposal.parent_unit_id, unit_id=child,
                position=position, extent="partial", origin="provider",
            )
        )  # fmt: skip
        position += 1
        added += 1
    return added
