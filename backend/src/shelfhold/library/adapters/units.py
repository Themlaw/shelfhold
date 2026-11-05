"""What the database says about a Unit's survival: irreplaceable, and what may be cleaned."""

from collections.abc import Sequence
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
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.library.domain.irreplaceable import (
    VariantState,
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
