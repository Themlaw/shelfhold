"""What the database says about a Unit's survival: irreplaceable, and what may be cleaned."""

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import Connection

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


def require_deletion_allowed(connection: Connection, unit_id: str, *, typed: str | None) -> None:
    """Every deletion of a Unit passes here first (EF-BIB-031): an irreplaceable one needs its
    identifier typed, from whoever asks (a retention policy cannot type)."""
    require_typed_confirmation(
        unit_id,
        irreplaceable=irreplaceability(connection, unit_id).irreplaceable,
        typed=typed,
    )


def safe_to_clean(connection: Connection, unit_ids: Sequence[str]) -> tuple[list[str], list[str]]:
    """(the Units a retention policy or a clean-up may touch, the ones it must keep and flag)."""
    allowed: list[str] = []
    kept: list[str] = []
    for unit_id in unit_ids:
        (kept if irreplaceability(connection, unit_id).irreplaceable else allowed).append(unit_id)
    return allowed, kept
