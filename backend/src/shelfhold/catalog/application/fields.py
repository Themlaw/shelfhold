"""What providers propose, kept with its provenance (EF-BIB-008, EF-BIB-055).

Fields come as the `FieldProposal` of the SDK (L11). Each one goes to the entity of its type the
host asked about, into `metadata_field`, with where it comes from. A value an admin locked is never
replaced: the proposal is ignored and the locked value keeps its provenance. An admin's value
(`lock_field`) is `manual` and locked; unlocking it keeps the value until a provider proposes
another one. A suggested age is never a rating: it is stored as a suggestion, and the removal plan
of the shelves below 18+ it now blocks is returned, never applied.

The caller owns the transaction.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.catalog.application.rating import RatingService, Withdrawal
from shelfhold.catalog.domain.provenance import (
    UNDETERMINED_LANG,
    FieldDecision,
    FieldKey,
    StoredField,
    decide,
    encode_value,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from shelfhold.sdk.age import AgeRating, to_columns
from shelfhold.sdk.metadata import FieldProposal

__all__ = [
    "ADMIN_PROVIDER",
    "FieldReport",
    "lock_current",
    "lock_field",
    "propose_fields",
    "read_field",
    "restore_field",
    "suggest_age",
    "unlock_field",
]

ADMIN_PROVIDER = "admin"  # the provider of a value an admin typed


@dataclass(frozen=True)
class FieldReport:
    written: tuple[FieldKey, ...]  # inserted or replaced
    kept_locked: tuple[FieldKey, ...]  # locked: the proposal was ignored
    skipped: int = 0  # about an entity type the host did not ask about


def _where(key: FieldKey) -> sa.ColumnElement[bool]:
    t = catalog.metadata_field
    return sa.and_(
        t.c.entity_type == key.entity_type,
        t.c.entity_id == key.entity_id,
        t.c.field == key.field,
        t.c.lang == key.lang,
    )


def read_field(connection: Connection, key: FieldKey) -> StoredField | None:
    t = catalog.metadata_field
    row = connection.execute(
        sa.select(
            t.c.value, t.c.provider, t.c.provenance, t.c.confidence, t.c.locked, t.c.fetched_at
        ).where(_where(key))
    ).first()
    if row is None:
        return None
    return StoredField(
        row.value, row.provider, row.provenance, row.confidence, bool(row.locked), row.fetched_at
    )


def restore_field(connection: Connection, key: FieldKey, stored: StoredField | None) -> None:
    """Put a field back as it was (`None`: absent), provenance and lock included."""
    connection.execute(sa.delete(catalog.metadata_field).where(_where(key)))
    if stored is not None:
        connection.execute(
            sa.insert(catalog.metadata_field).values(
                entity_type=key.entity_type,
                entity_id=key.entity_id,
                field=key.field,
                lang=key.lang,
                value=stored.value,
                provider=stored.provider,
                provenance=stored.provenance,
                confidence=stored.confidence,
                locked=stored.locked,
                fetched_at=stored.fetched_at,
            )
        )


def _write(connection: Connection, key: FieldKey, stored: StoredField) -> None:
    t = catalog.metadata_field
    values = {
        "value": stored.value,
        "provider": stored.provider,
        "provenance": stored.provenance,
        "confidence": stored.confidence,
        "locked": stored.locked,
        "fetched_at": stored.fetched_at,
    }
    connection.execute(
        sqlite_insert(t)
        .values(
            entity_type=key.entity_type,
            entity_id=key.entity_id,
            field=key.field,
            lang=key.lang,
            **values,
        )
        .on_conflict_do_update(
            index_elements=[t.c.entity_type, t.c.entity_id, t.c.field, t.c.lang], set_=values
        )
    )


def propose_fields(
    connection: Connection,
    targets: Mapping[str, str],
    proposals: Iterable[FieldProposal],
    *,
    now: datetime,
    origin: str = "provider",
) -> FieldReport:
    """Store each proposal on the entity of its type in `targets` (entity type -> id).

    A locked value is never replaced; the same value from the same provider writes nothing.
    `origin` is the kind of provenance (`provider`, `source`, `embedded`, `import`)."""
    written: list[FieldKey] = []
    locked: list[FieldKey] = []
    skipped = 0
    for proposal in proposals:
        entity_id = targets.get(proposal.entity_type)
        if entity_id is None:
            skipped += 1
            continue
        key = FieldKey(
            proposal.entity_type,
            entity_id,
            proposal.field,
            str(proposal.lang) if proposal.lang else UNDETERMINED_LANG,
        )
        value, provider = encode_value(proposal.value), proposal.provenance.provider
        decision = decide(read_field(connection, key), value, provider)
        if decision is FieldDecision.KEEP_LOCKED:
            locked.append(key)
            continue
        if decision is FieldDecision.SAME:
            continue
        _write(
            connection,
            key,
            StoredField(
                value,
                provider,
                origin,
                proposal.confidence,
                False,
                proposal.provenance.fetched_at or now,
            ),
        )
        written.append(key)
    return FieldReport(tuple(written), tuple(locked), skipped)


def lock_field(connection: Connection, key: FieldKey, value: object, *, now: datetime) -> None:
    """An admin's value: `manual`, locked; no provider replaces it."""
    _write(
        connection, key, StoredField(encode_value(value), ADMIN_PROVIDER, "manual", None, True, now)
    )


def lock_current(connection: Connection, key: FieldKey) -> bool:
    """Lock the value that is stored, whoever gave it: its provider and provenance are kept.
    `False` when nothing is stored for that field (there is nothing to lock)."""
    done = connection.execute(
        sa.update(catalog.metadata_field).where(_where(key)).values(locked=True)
    )
    return bool(done.rowcount)


def unlock_field(connection: Connection, key: FieldKey) -> bool:
    """Lift the lock; the value and its provenance stay until a provider proposes another."""
    done = connection.execute(
        sa.update(catalog.metadata_field).where(_where(key)).values(locked=False)
    )
    return bool(done.rowcount)


def suggest_age(
    connection: Connection,
    entity_type: str,
    entity_id: str,
    age: AgeRating,
    *,
    provenance: str,
    now: datetime,
    rating: RatingService | None = None,
) -> tuple[Withdrawal, ...]:
    """A file, a sub-source or a provider suggests an age: stored as a suggestion (the same one
    twice is one row), never as a rating. Returns the removal plan of the Editions of the Work it
    now keeps out of a shelf below 18+ (EF-BIB-055); nothing is withdrawn here."""
    value, x = to_columns(age)
    table = catalog.age_suggestion
    connection.execute(
        sqlite_insert(table)
        .values(
            id=new_ulid(),
            entity_type=entity_type,
            entity_id=entity_id,
            value=value,
            x=x,
            provenance=provenance,
            seen_at=now,
        )
        .on_conflict_do_nothing()
    )
    work_id = (
        entity_id
        if entity_type == "work"
        else connection.execute(
            sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == entity_id)
        ).scalar_one()
    )
    return (rating or RatingService()).withdrawals(connection, work_id=work_id)


def keys_of(proposals: Sequence[FieldProposal], targets: Mapping[str, str]) -> list[FieldKey]:
    """The keys the proposals would write, in their order (an attach snapshots them first)."""
    return [
        FieldKey(
            p.entity_type,
            targets[p.entity_type],
            p.field,
            str(p.lang) if p.lang else UNDETERMINED_LANG,
        )
        for p in proposals
        if p.entity_type in targets
    ]
