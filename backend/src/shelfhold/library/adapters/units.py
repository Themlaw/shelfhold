"""What the database says about a Unit's survival: irreplaceable, and what may be cleaned."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    Principal,
    Verdict,
    authorize_reserved,
)
from shelfhold.infrastructure.schema import acquisition, catalog, storage
from shelfhold.library.adapters.purge_inventory import erase_content_rows
from shelfhold.library.domain.irreplaceable import (
    ConfirmationRequiredError,
    VariantState,
    confirmation_phrase,
    is_candidate,
    is_irreplaceable,
    require_typed_confirmation,
)
from shelfhold.library.replica import independent_copies


@dataclass(frozen=True)
class Irreplaceability:
    irreplaceable: bool
    independent_copies: int  # what the registry holds (EF-BIB-045); 0 or 1 is the Health alarm


def irreplaceability(connection: Connection, unit_id: str) -> Irreplaceability:
    unit = connection.execute(
        sa.select(catalog.unit.c.availability).where(catalog.unit.c.id == unit_id)
    ).scalar_one()
    variant, binding = catalog.unit_variant, acquisition.source_binding
    states = [
        VariantState(binding_active=bool(row.active), removed=row.removed_at is not None)
        for row in connection.execute(
            sa.select(binding.c.active, variant.c.removed_at)
            .select_from(variant.join(binding, binding.c.id == variant.c.source_binding_id))
            .where(variant.c.unit_id == unit_id)
        )
    ]
    return Irreplaceability(is_irreplaceable(states, unit), independent_copies(connection, unit_id))


@dataclass(frozen=True)
class ContentErrorCandidate:
    unit_id: str
    source_key: str  # the upstream source whose content errors made it a candidate
    errors: int
    last_error_code: str | None
    independent_copies: int


def content_error_candidates(connection: Connection) -> list[ContentErrorCandidate]:
    """Units that hold a copy and whose live Variant sits on a binding whose own upstream source
    failed on content `is_candidate` times in a row (EF-BIB-042 (3), EF-BIB-031). Read only: a
    candidate is flagged, never copied, moved or deleted here."""
    failure, binding = acquisition.source_failure, acquisition.source_binding
    item, variant = acquisition.source_item, catalog.unit_variant
    rows = connection.execute(
        sa.select(
            variant.c.unit_id,
            failure.c.limit_key,
            failure.c.consecutive_content_errors,
            failure.c.last_error_code,
        )
        .select_from(
            failure.join(binding, binding.c.id == failure.c.source_binding_id)
            .join(
                item,
                sa.and_(
                    item.c.id == binding.c.source_item_id, item.c.source_key == failure.c.limit_key
                ),
            )
            .join(variant, variant.c.source_binding_id == binding.c.id)
        )
        .where(
            failure.c.failure_class == "content",
            failure.c.consecutive_content_errors > 0,
            variant.c.removed_at.is_(None),
            sa.exists().where(storage.content.c.variant_id == variant.c.id),
        )
        .distinct()
        .order_by(variant.c.unit_id)
    ).all()
    return [
        ContentErrorCandidate(
            r.unit_id,
            r.limit_key,
            r.consecutive_content_errors,
            r.last_error_code,
            independent_copies(connection, r.unit_id),
        )
        for r in rows
        if is_candidate(r.consecutive_content_errors)
    ]


class DeletionRefusedError(PermissionError):
    """The reserved-action rule refused the actor (a key, a stale session, a non-admin)."""

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(f"unit.delete refused: {type(verdict).__name__}")
        self.verdict = verdict


@dataclass(frozen=True)
class DeletionAuthorization:
    unit_id: str
    actor: str  # `Principal.actor_id`: one session or the host CLI, for the audit entry
    irreplaceable: bool


def require_deletion_allowed(
    connection: Connection,
    unit_id: str,
    *,
    principal: Principal,
    now: datetime,
    typed: str | None,
    fresh_auth_s: int = 900,
) -> DeletionAuthorization:
    """Every deletion of a Unit passes here first (EF-BIB-031).

    Deleting a Unit's copies deletes for good, so it is a reserved action (`unit.delete`): refused
    to any key or token, to a non-admin session and to a stale one (fresh authentication). On top
    of that, an irreplaceable Unit needs its identifier typed; a retention policy cannot type.
    Returns who is authorised, for the audit entry the caller writes."""
    verdict = authorize_reserved(
        principal, RESERVED_ACTIONS["unit.delete"], now=now, fresh_auth_s=fresh_auth_s
    )
    if not isinstance(verdict, Allowed):
        raise DeletionRefusedError(verdict)
    irreplaceable = irreplaceability(connection, unit_id).irreplaceable
    require_typed_confirmation(unit_id, irreplaceable=irreplaceable, typed=typed)
    return DeletionAuthorization(unit_id, principal.actor_id, irreplaceable)


def safe_to_clean(connection: Connection, unit_ids: Sequence[str]) -> tuple[list[str], list[str]]:
    """(the Units a retention policy or a clean-up may touch, the ones it must keep and flag)."""
    allowed: list[str] = []
    kept: list[str] = []
    for unit_id in unit_ids:
        (kept if irreplaceability(connection, unit_id).irreplaceable else allowed).append(unit_id)
    return allowed, kept


class PurgeRefusedError(PermissionError):
    """The reserved-action rule refused the actor (a key, a stale session, a non-admin)."""

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(f"purge.now refused: {type(verdict).__name__}")
        self.verdict = verdict


@dataclass(frozen=True)
class PurgeAuthorization:
    """What `erase_units` asks for: who was authorised to purge what (EF-BIB-052). Only
    `require_purge_allowed` makes one."""

    anchor_id: str  # the Unit, or the Work, whose identifier the admin typed
    actor: str  # `Principal.actor_id`, for the audit entry


def require_purge_allowed(
    anchor_id: str,
    *,
    principal: Principal,
    now: datetime,
    typed: str | None,
    fresh_auth_s: int = 900,
) -> PurgeAuthorization:
    """Every purge passes here first, before anything is written (not even a tombstone).

    `purge.now` is a reserved action: a fresh admin session or the host CLI, never a key. On top
    of that the admin types the identifier, whether the Unit is irreplaceable or not: a purge
    deletes the last copy, past the bin and past "Mettre à l'abri"."""
    verdict = authorize_reserved(
        principal, RESERVED_ACTIONS["purge.now"], now=now, fresh_auth_s=fresh_auth_s
    )
    if not isinstance(verdict, Allowed):
        raise PurgeRefusedError(verdict)
    if typed != confirmation_phrase(anchor_id):
        raise ConfirmationRequiredError(anchor_id)
    return PurgeAuthorization(anchor_id, principal.actor_id)


def erase_units(
    connection: Connection,
    unit_ids: Collection[str],
    export_item_ids: Collection[str],
    *,
    authorization: PurgeAuthorization,
) -> None:
    """The rows of purged Units: contents, copies, bin items, Variants (their revisions go with
    them), then the Units. The only place a `unit` row is deleted. The files are gone before the
    caller gets here (EF-BIB-052, step 5)."""
    del authorization  # the proof that `require_purge_allowed` ran: nothing else makes one
    erase_content_rows(connection, unit_ids, export_item_ids)
    connection.execute(
        sa.delete(catalog.unit_variant).where(catalog.unit_variant.c.unit_id.in_(unit_ids))
    )
    connection.execute(sa.delete(catalog.unit).where(catalog.unit.c.id.in_(unit_ids)))
