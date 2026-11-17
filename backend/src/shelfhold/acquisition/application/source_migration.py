"""The migration of a series to another source (EF-BIB-014, M4 L27): a numbering correspondence,
proposed, validated as it was shown, then applied in one transaction.

A series moves to a source that numbers its chapters otherwise. It is the same Edition: the Units it
holds stay the Units (same id, same number, same file), and the history and progress of every
profile, which live on them, do not move. What changes is which binding lists it, and how the
numbers of the new source read in the library's own numbering. That reading is the
**correspondence**:

- *proposed* from what the new source lists: the identical numbers, else one constant offset (the
  new source's 11 is the library's 1). Every line says the Unit it lands on, or that the library
  lacks it (to fetch, under the library's numbering: never a second Unit for a number it holds);
- *validated* by the admin, as shown: the digest he sends is the one of the table he saw, and a
  line he changes by hand is checked again (two numbers never land on one place);
- *applied* under the write lock, only against the Edition the table was made for (a Unit that
  came or went since makes it stale), as one transaction: the binding (`relink`: the Team and its
  aliases follow), the table the follow cycle reads, the audit line. A cut at any step leaves the
  old state, and the same call made again gives the same result.

Nothing here fetches: the next follow cycle lists the new source, reads its numbers through the
correspondence (`NumberMap`), and so asks for the numbers the library lacks and for nothing else.
A key, a device token, a member or a child never reaches any step (`MigrationNotAllowedError`),
and a table is only read or changed through the Edition it was made for.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.public import (
    ListedUnit,
    RelinkRequest,
    RelinkService,
    place_key,
    unit_place_key,
)
from shelfhold.identity.public import AuditEntry, Credential, Principal, audit_insert
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog

__all__ = [
    "ACTION",
    "APPLY_STEPS",
    "CollisionError",
    "InvalidMigrationError",
    "MigrationNotAllowedError",
    "MigrationOutcome",
    "MigrationProposal",
    "MigrationRow",
    "NotValidatedError",
    "NumberMap",
    "Override",
    "SourceMigrationError",
    "SourceMigrations",
    "StaleMigrationError",
    "UnknownMigrationError",
    "number_map_of",
]

ACTION = "source.migrate"
# The points of `apply` between which a cut may fall (a test cuts at each one): after the checks,
# the binding, the table the follow cycle reads, the audit line. The first three write nothing
# that is kept unless the transaction commits: there is one transaction.
APPLY_STEPS = ("checked", "relinked", "mapped", "audited")
MAX_ROWS = 20_000  # a listing longer than this is no series of chapters
MAX_OVERRIDES = 2_000
_PLAIN = re.compile(r"^\d{1,12}(\.\d{1,6})?$")  # a number that a constant offset can move
_MIN_MATCHES = 3  # an offset must explain at least this many held numbers to be proposed


class SourceMigrationError(Exception):
    """The base of every refusal of a migration."""


class MigrationNotAllowedError(SourceMigrationError):
    """Only an admin session or the host's CLI may migrate: never a key, a token, a member."""


class UnknownMigrationError(SourceMigrationError, LookupError):
    """No such Edition, or no such table under that Edition."""


class NotValidatedError(SourceMigrationError):
    """A table is applied only once the admin validated it."""


class StaleMigrationError(SourceMigrationError):
    """The table is not the one that was shown, or the Edition is not the one it was made for."""


class InvalidMigrationError(SourceMigrationError):
    """The request names something that is not there (a number, a target, a listing)."""


class CollisionError(SourceMigrationError):
    """Two numbers of the new source land on one place of the library."""

    def __init__(self, places: Sequence[str]) -> None:
        super().__init__("two numbers land on one place")
        self.places = tuple(places)  # the numbers of the new source, as it writes them


@dataclass(frozen=True)
class Override:
    """The admin's own answer for one line: `number` (as the new source writes it) takes
    `target`, a number of the library's own numbering."""

    number: str
    target: str
    # a line without a number is named by its place (the id of the line), and its `target` is then
    # the id of the held Unit it is paired with
    place: str | None = None


@dataclass(frozen=True)
class MigrationRow:
    place: str  # the number of the new source, placed as the follow cycle places it
    number: str | None  # ... as the new source writes it
    source_order: int
    target: str | None  # the number it takes in the library; None for an extra without one
    target_order: int
    target_place: str
    unit_id: str | None  # the Unit it lands on, or None: the library lacks it
    basis: str  # same_number | offset | manual | new


@dataclass(frozen=True)
class MigrationProposal:
    id: str
    edition_id: str
    state: str
    digest: str
    offset: int
    rows: tuple[MigrationRow, ...]
    unmatched_units: tuple[str, ...]  # held Units that no number of the new source reaches
    held_count: int
    plugin_id: str = ""
    source_key: str = ""
    remote_id: str = ""
    binding_id: str | None = None

    @property
    def to_fetch(self) -> tuple[MigrationRow, ...]:
        """The numbers the library lacks: the only ones to ask the new source for."""
        return tuple(r for r in self.rows if r.unit_id is None)


@dataclass(frozen=True)
class MigrationOutcome:
    proposal: MigrationProposal
    binding_id: str
    already: bool  # a replay: the table was applied before, nothing was done now

    @property
    def to_fetch(self) -> tuple[MigrationRow, ...]:
        return self.proposal.to_fetch


@dataclass(frozen=True)
class _Held:
    id: str
    number_raw: str
    source_order: int


class NumberMap:
    """How the numbers of a migrated binding read in the library's numbering: the follow cycle
    translates each release it ingests, so an offer lands on the Unit that holds that number."""

    def __init__(self, rows: dict[str, MigrationRow], offset: int) -> None:
        self._rows = rows
        self._offset = offset
        self._taken = {r.target_place for r in rows.values()}
        # the offset is only trusted for a release that the table does not list when it is the one
        # every line of the table follows: a table with a line moved by hand, or a partial offset,
        # says nothing of what the source will publish next
        gaps = {g for r in rows.values() if (g := _gap(r)) is not None}
        self._uniform = gaps <= {offset}

    def held_aside(self, raw_number: str | None, source_order: int) -> bool:
        """A release that the table does not list and that cannot be placed safely: it has no
        number (its rank in another source's list says nothing), or the offset is not the one of
        the whole table, or it would land on a place that a line of the table already takes."""
        if place_key(raw_number, source_order) in self._rows:
            return False
        if raw_number is None or not self._uniform:
            return True
        number, order = self._read(raw_number, source_order)
        return place_key(number, order) in self._taken

    def _read(self, raw_number: str, source_order: int) -> tuple[str, int]:
        if self._offset and _PLAIN.match(raw_number.strip()):
            shifted = _shift(raw_number.strip(), self._offset)
            if shifted is not None:
                return shifted, source_order
        return raw_number, source_order

    def translate(self, raw_number: str | None, source_order: int) -> tuple[str | None, int]:
        """The library's number for this release; `(None, order)` for one held aside (see
        `held_aside`): it has no library number until the admin gives it one."""
        row = self._rows.get(place_key(raw_number, source_order))
        if row is not None:
            return row.target, row.target_order
        if raw_number is None or self.held_aside(raw_number, source_order):
            return None, source_order
        return self._read(raw_number, source_order)


def _gap(row: MigrationRow) -> int | None:
    """How far a line moves its number (None for a line that is no plain number)."""
    have, want = _plain_value(row.number), _plain_value(row.target)
    if have is None or want is None:
        return None
    gap = want - have
    return int(gap) if gap == gap.to_integral_value() else None


def number_map_of(connection: Connection, binding_id: str) -> NumberMap | None:
    """The correspondence applied for this binding, or None: its numbers are the library's own."""
    migration = acquisition.source_migration
    found = connection.execute(
        sa.select(migration.c.id, migration.c.number_offset).where(
            migration.c.binding_id == binding_id, migration.c.state == "applied"
        )
    ).first()
    if found is None:
        return None
    rows = {r.place: r for r in _load_rows(connection, str(found.id))}
    return NumberMap(rows, int(found.number_offset))


class SourceMigrations:
    """Propose, read, validate and apply. The caller owns the transaction, and holds the write
    lock for `validate` and `apply` (what is checked is what is written)."""

    def __init__(
        self,
        relinker: RelinkService | None = None,
        *,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._relinker = relinker
        self._on_step = checkpoint or (lambda _step: None)

    def _relink(self) -> RelinkService:
        if self._relinker is None:
            self._relinker = RelinkService()
        return self._relinker

    # -- propose -------------------------------------------------------------------------------
    def propose(
        self,
        connection: Connection,
        edition_id: str,
        *,
        plugin_id: str,
        source_key: str,
        remote_id: str,
        listed: Sequence[ListedUnit],
        principal: Principal,
        now: datetime,
        replaces_binding_id: str | None = None,
        memo: dict[str, object] | None = None,
        memo_version: int | None = None,
    ) -> MigrationProposal:
        _require_admin(principal)
        if not plugin_id or not source_key or not remote_id:
            raise InvalidMigrationError("name the new source and its series")
        if not listed or len(listed) > MAX_ROWS:
            raise InvalidMigrationError("the new source lists no chapter, or too many")
        work_id = _work_of(connection, edition_id)
        if replaces_binding_id is not None:
            _replaced(connection, edition_id, replaces_binding_id)
        held = _held(connection, edition_id)
        offset = _best_offset(held, listed)
        rows = _build(held, listed, offset)
        digest = _digest(
            edition_id, (plugin_id, source_key, remote_id), replaces_binding_id, offset, rows,
            held,
        )  # fmt: skip
        table = acquisition.source_migration
        open_one = connection.execute(
            sa.select(table.c.id).where(
                table.c.edition_id == edition_id,
                table.c.digest == digest,
                table.c.state.in_(("proposed", "validated")),
            )
        ).scalar()
        if open_one is not None:  # the same listing proposed again is the same table
            return self._read(connection, edition_id, str(open_one))
        migration_id = new_ulid()
        connection.execute(
            sa.insert(table).values(
                id=migration_id, edition_id=edition_id, work_id=work_id, plugin_id=plugin_id,
                source_key=source_key, remote_id=remote_id, memo=memo or {},
                memo_version=memo_version, replaces_binding_id=replaces_binding_id,
                state="proposed", number_offset=offset, digest=digest,
                held_digest=_held_digest(held), created_at=now,
            )
        )  # fmt: skip
        _write_rows(connection, migration_id, rows)
        self._audit(connection, principal, now, edition_id, migration_id, "propose", rows)
        return self._read(connection, edition_id, migration_id)

    # -- read ----------------------------------------------------------------------------------
    def get(
        self, connection: Connection, edition_id: str, migration_id: str, *, principal: Principal
    ) -> MigrationProposal:
        _require_admin(principal)
        return self._read(connection, edition_id, migration_id)

    def _read(
        self, connection: Connection, edition_id: str, migration_id: str
    ) -> MigrationProposal:
        table = acquisition.source_migration
        head = connection.execute(
            sa.select(table).where(table.c.id == migration_id, table.c.edition_id == edition_id)
        ).first()
        if head is None:
            raise UnknownMigrationError("no such table under that Edition")
        rows = tuple(_load_rows(connection, migration_id))
        held = _held(connection, edition_id)
        reached = {r.unit_id for r in rows if r.unit_id}
        return MigrationProposal(
            id=str(head.id), edition_id=edition_id, state=str(head.state), digest=str(head.digest),
            offset=int(head.number_offset), rows=rows,
            unmatched_units=tuple(
                sorted(h.id for h in held.values() if h.id not in reached)
            ),
            held_count=len(held), plugin_id=str(head.plugin_id),
            source_key=str(head.source_key), remote_id=str(head.remote_id),
            binding_id=None if head.binding_id is None else str(head.binding_id),
        )  # fmt: skip

    # -- validate ------------------------------------------------------------------------------
    def validate(
        self,
        connection: Connection,
        edition_id: str,
        migration_id: str,
        *,
        digest: str,
        overrides: Sequence[Override],
        principal: Principal,
        now: datetime,
        listed: Sequence[ListedUnit] | None = None,
    ) -> MigrationProposal:
        """The admin validates the table he was shown (`digest` is that table's), with the lines
        he changed. Every line is checked again; a collision is refused and nothing is written."""
        _require_admin(principal)
        if len(overrides) > MAX_OVERRIDES:
            raise InvalidMigrationError("too many lines changed at once")
        current = self._read(connection, edition_id, migration_id)
        if not _same(digest, current.digest):
            raise StaleMigrationError("that is not the table that was shown")
        if current.state == "validated" and not overrides:
            return current  # replayed
        if current.state != "proposed":
            raise StaleMigrationError(f"a table that is {current.state} is not validated again")
        self._fresh(connection, edition_id, migration_id)
        _still_listed(current.rows, listed)
        held = _held(connection, edition_id)
        rows = _override(held, current.rows, overrides)
        new_digest = _digest(
            edition_id, (current.plugin_id, current.source_key, current.remote_id),
            _replaces_of(connection, migration_id), current.offset, rows, held,
        )  # fmt: skip
        if new_digest != current.digest:
            _write_rows(connection, migration_id, rows)
        table = acquisition.source_migration
        connection.execute(
            sa.update(table)
            .where(table.c.id == migration_id)
            .values(state="validated", digest=new_digest, validated_at=now)
        )
        self._audit(connection, principal, now, edition_id, migration_id, "validate", rows)
        return self._read(connection, edition_id, migration_id)

    # -- apply ---------------------------------------------------------------------------------
    def _reached(self, step: str) -> None:
        """Tell the hook (a test's cut) that a step of `APPLY_STEPS` was just done."""
        if step not in APPLY_STEPS:
            raise ValueError(f"unknown step of an application: {step}")
        self._on_step(step)

    def apply(
        self,
        connection: Connection,
        edition_id: str,
        migration_id: str,
        *,
        digest: str,
        principal: Principal,
        now: datetime,
        listed: Sequence[ListedUnit] | None = None,
    ) -> MigrationOutcome:
        _require_admin(principal)
        current = self._read(connection, edition_id, migration_id)
        if not _same(digest, current.digest):
            raise StaleMigrationError("that is not the table that was validated")
        if current.state == "applied" and current.binding_id is not None:
            return MigrationOutcome(current, current.binding_id, already=True)  # a replay
        if current.state != "validated":
            raise NotValidatedError("validate the table before applying it")
        self._fresh(connection, edition_id, migration_id)
        _still_listed(current.rows, listed)
        self._reached("checked")
        head = connection.execute(
            sa.select(acquisition.source_migration).where(
                acquisition.source_migration.c.id == migration_id
            )
        ).one()
        done = self._relink().relink(
            connection,
            RelinkRequest(
                work_id=str(head.work_id), plugin_id=str(head.plugin_id),
                source_key=str(head.source_key), remote_id=str(head.remote_id), listed=(),
                edition_id=edition_id,
                replaces_binding_id=None if head.replaces_binding_id is None
                else str(head.replaces_binding_id),
                memo=dict(head.memo or {}), memo_version=head.memo_version, inherit_rank=True,
            ),
            principal=principal, now=now,
        )  # fmt: skip
        self._reached("relinked")
        table = acquisition.source_migration
        connection.execute(  # one migrated binding by series: the other open tables are closed
            sa.update(table)
            .where(
                table.c.edition_id == edition_id,
                table.c.id != migration_id,
                table.c.state.in_(("proposed", "validated")),
            )
            .values(state="cancelled")
        )
        connection.execute(
            sa.update(table)
            .where(table.c.id == migration_id)
            .values(state="applied", binding_id=done.binding_id, applied_at=now)
        )
        self._reached("mapped")
        self._audit(connection, principal, now, edition_id, migration_id, "apply", current.rows)
        self._reached("audited")
        return MigrationOutcome(
            replace(current, state="applied", binding_id=done.binding_id), done.binding_id,
            already=False,
        )  # fmt: skip

    # -- shared --------------------------------------------------------------------------------
    @staticmethod
    def _fresh(connection: Connection, edition_id: str, migration_id: str) -> None:
        """The table is checked against the Edition as it is now."""
        table = acquisition.source_migration
        stored = connection.execute(
            sa.select(table.c.held_digest).where(table.c.id == migration_id)
        ).scalar_one()
        if stored != _held_digest(_held(connection, edition_id)):
            raise StaleMigrationError("the Edition changed since that table was made")

    @staticmethod
    def _audit(
        connection: Connection, principal: Principal, now: datetime, edition_id: str,
        migration_id: str, step: str, rows: Sequence[MigrationRow],
    ) -> None:  # fmt: skip
        """One line per step done, in the transaction that does it: counts and ids, never a title
        or a source's name."""
        fetch = sum(1 for r in rows if r.unit_id is None)
        connection.execute(
            audit_insert(
                AuditEntry(
                    actor_id=principal.actor_id, action=ACTION, at=now,
                    target=f"edition:{edition_id};mig:{migration_id};{step};rows:{len(rows)};"
                    f"fetch:{fetch}",
                    session_id=principal.session_id, auth_time=principal.auth_time,
                )
            )
        )  # fmt: skip


# -- the rules (pure over what is read) ----------------------------------------------------------


def _require_admin(principal: Principal) -> None:
    if principal.role != "admin" or principal.credential in (
        Credential.API_KEY,
        Credential.DEVICE_TOKEN,
    ):
        raise MigrationNotAllowedError(f"{principal.credential.value}/{principal.role}")


def _still_listed(rows: Sequence[MigrationRow], listed: Sequence[ListedUnit] | None) -> None:
    """The source, read again, still lists every line of the table (what it lists more is read
    by the offset); a source that renumbered since is no source this table was made for."""
    if listed is None:
        return
    now = {place_key(u.raw_number, u.source_order) for u in listed}
    if any(r.place not in now for r in rows):
        raise StaleMigrationError("the source lists the series otherwise than when it was shown")


def _same(sent: str, stored: str) -> bool:
    import hmac

    return hmac.compare_digest(sent.encode(), stored.encode())


def _plain_value(raw: str | None) -> Decimal | None:
    if raw is None or not _PLAIN.match(raw.strip()):
        return None
    return Decimal(raw.strip())


def _shift(raw: str, offset: int) -> str | None:
    """`raw` moved by `offset`, written plainly; None when it would not be a number any more."""
    try:
        value = Decimal(raw) + offset
    except InvalidOperation:  # pragma: no cover (the caller matched `_PLAIN`)
        return None
    if value < 0:
        return None
    text = format(value.normalize(), "f")
    return text


def _held(connection: Connection, edition_id: str) -> dict[str, _Held]:
    unit = catalog.unit
    found: dict[str, _Held] = {}
    for row in connection.execute(
        sa.select(
            unit.c.id, unit.c.number_raw, unit.c.source_order, unit.c.track,
            unit.c.number_segment, unit.c.number_norm, unit.c.part,
        )
        .where(unit.c.edition_id == edition_id)
        .order_by(unit.c.number_sort, unit.c.id)
    ):  # fmt: skip
        key = unit_place_key(
            row.track, row.number_segment, row.number_norm, row.part, row.source_order
        )
        found[key] = _Held(str(row.id), str(row.number_raw), int(row.source_order))
    return found


def _held_digest(held: dict[str, _Held]) -> str:
    return _sha(sorted((place, h.id) for place, h in held.items()))


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _digest(
    edition_id: str, source: tuple[str, str, str], replaces: str | None, offset: int,
    rows: Sequence[MigrationRow], held: dict[str, _Held],
) -> str:  # fmt: skip
    return _sha(
        [
            edition_id, list(source), replaces, offset, _held_digest(held),
            sorted(
                (r.place, r.number, r.source_order, r.target, r.target_order, r.target_place,
                 r.unit_id, r.basis)
                for r in rows
            ),
        ]
    )  # fmt: skip


def _best_offset(held: dict[str, _Held], listed: Sequence[ListedUnit]) -> int:
    """A constant offset that explains clearly more held numbers than the identical numbers do,
    else 0. Only plain numbers are moved; a shift that would make one negative is no offset."""
    have = {v for h in held.values() if (v := _plain_value(h.number_raw)) is not None}
    want = [v for u in listed if (v := _plain_value(u.raw_number)) is not None]
    if not have or not want:
        return 0
    exact = len(have & set(want))
    steps: set[int] = set()
    for held_value in (min(have), max(have)):
        for value in (min(want), max(want)):
            gap = held_value - value
            if gap == gap.to_integral_value() and gap != 0:
                steps.add(int(gap))
    best, best_matches = 0, exact
    for step in sorted(steps, key=lambda k: (abs(k), k)):
        if min(want) + step < 0:
            continue  # a number would go negative: that is no offset
        shifted = {v + step for v in want}
        if not have <= shifted:
            continue  # a held number that the shift does not reach: no offset of the whole series
        matches = len(have & shifted)
        if matches > best_matches:
            best, best_matches = step, matches
    if best == 0 or best_matches < min(_MIN_MATCHES, len(have)):
        return 0
    return best


def _build(
    held: dict[str, _Held], listed: Sequence[ListedUnit], offset: int
) -> tuple[MigrationRow, ...]:
    """One line per place of the new source (a number listed twice is one line), landing where
    the library already holds that number, or under the library's own number when it lacks it."""
    rows: dict[str, MigrationRow] = {}
    for item in listed:
        place = place_key(item.raw_number, item.source_order)
        if place in rows:
            continue
        number = item.raw_number
        target, order, moved = number, item.source_order, False
        if offset and number is not None and _PLAIN.match(number.strip()):
            shifted = _shift(number.strip(), offset)
            if shifted is not None:
                target, moved = shifted, True
        # a chapter without a number is never paired by its rank: the admin pairs it
        found = held.get(place_key(target, order)) if number is not None else None
        if found is not None:
            target, order = found.number_raw, found.source_order
        rows[place] = MigrationRow(
            place=place, number=number, source_order=item.source_order, target=target,
            target_order=order, target_place=place_key(target, order),
            unit_id=None if found is None else found.id,
            basis="new" if found is None else ("offset" if moved else "same_number"),
        )  # fmt: skip
    chosen = tuple(rows.values())
    clashes = _collisions(chosen)
    if clashes and offset:  # an offset that makes two numbers one is no offset
        return _build(held, listed, 0)
    if clashes:  # pragma: no cover (identical places are one line: nothing can clash)
        raise CollisionError(clashes)
    return chosen


def _collisions(rows: Sequence[MigrationRow]) -> list[str]:
    seen: dict[str, str] = {}
    clash: list[str] = []
    for row in rows:
        shown = row.number if row.number is not None else row.place
        if row.target_place in seen:
            clash += [seen[row.target_place], shown]
        else:
            seen[row.target_place] = shown
    return sorted(set(clash))


def _override(
    held: dict[str, _Held], rows: Sequence[MigrationRow], overrides: Sequence[Override]
) -> tuple[MigrationRow, ...]:
    by_number = {r.number: r for r in rows if r.number is not None}
    by_place = {r.place: r for r in rows}
    changed = {r.place: r for r in rows}
    by_id = {h.id: (place, h) for place, h in held.items()}
    for item in overrides:
        if item.place is not None:
            row = by_place.get(item.place)
            if row is None or row.number is not None:
                raise InvalidMigrationError("a line that is not in the table")
            if item.target not in by_id:
                raise InvalidMigrationError("a Unit that is not of this Edition")
            _, unit = by_id[item.target]
            changed[row.place] = replace(
                row, target=unit.number_raw, target_order=unit.source_order,
                target_place=by_id[item.target][0], unit_id=unit.id, basis="manual",
            )  # fmt: skip
            continue
        row = by_number.get(item.number)
        if row is None:
            raise InvalidMigrationError("a line that is not in the table")
        target = item.target.strip()
        if not target or place_key(target, 0).startswith("unnumbered:"):
            raise InvalidMigrationError("a target must be a number of the library")
        found = held.get(place_key(target, 0))
        unit_target, unit_order = (found.number_raw, found.source_order) if found else (target, 0)
        changed[row.place] = replace(
            row, target=unit_target, target_order=unit_order,
            target_place=place_key(unit_target, unit_order),
            unit_id=None if found is None else found.id, basis="manual",
        )  # fmt: skip
    result = tuple(changed.values())
    clashes = _collisions(result)
    if clashes:
        raise CollisionError(clashes)
    return result


def _work_of(connection: Connection, edition_id: str) -> str:
    found = connection.execute(
        sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == edition_id)
    ).scalar()
    if found is None:
        raise UnknownMigrationError("no such Edition")
    return str(found)


def _replaced(connection: Connection, edition_id: str, binding_id: str) -> None:
    binding = acquisition.source_binding
    found = connection.execute(
        sa.select(binding.c.id).where(
            binding.c.id == binding_id, binding.c.edition_id == edition_id
        )
    ).first()
    if found is None:
        raise InvalidMigrationError("that binding is not one of this Edition")


def _replaces_of(connection: Connection, migration_id: str) -> str | None:
    table = acquisition.source_migration
    value = connection.execute(
        sa.select(table.c.replaces_binding_id).where(table.c.id == migration_id)
    ).scalar()
    return None if value is None else str(value)


def _load_rows(connection: Connection, migration_id: str) -> list[MigrationRow]:
    line = acquisition.source_migration_row
    return [
        MigrationRow(
            place=str(r.place),
            number=r.number,
            source_order=int(r.source_order),
            target=r.target,
            target_order=int(r.target_order),
            target_place=str(r.target_place),
            unit_id=None if r.unit_id is None else str(r.unit_id),
            basis=str(r.basis),
        )
        for r in connection.execute(
            sa.select(line)
            .where(line.c.migration_id == migration_id)
            .order_by(line.c.source_order, line.c.place)
        )
    ]


def _write_rows(connection: Connection, migration_id: str, rows: Sequence[MigrationRow]) -> None:
    line = acquisition.source_migration_row
    connection.execute(sa.delete(line).where(line.c.migration_id == migration_id))
    connection.execute(
        sa.insert(line),
        [
            {
                "migration_id": migration_id, "place": r.place, "number": r.number,
                "source_order": r.source_order, "target": r.target,
                "target_order": r.target_order, "target_place": r.target_place,
                "unit_id": r.unit_id, "basis": r.basis,
            }
            for r in rows
        ],
    )  # fmt: skip
