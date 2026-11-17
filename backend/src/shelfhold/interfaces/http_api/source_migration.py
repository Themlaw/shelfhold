"""`/api/v1/editions/{edition_id}/source-migrations`: the migration of a series to another source
(EF-BIB-014, M4 L27).

Four routes under one Edition, all of the one reserved action `source.migrate`: a fresh admin
session, never an API key (the `admin` scope and a read-only one alike), never a device token, a
member or a child. The refusal comes before anything is looked at, so it says nothing of which
Editions or tables exist.

- `POST` proposes the numbering correspondence for a series the connected service follows under
  another source: a table, one line per number of the new source, the Unit it lands on or "new".
  Nothing else is written, and the same listing proposed again is the same table;
- `GET .../{id}` reads it;
- `POST .../{id}/validation` validates the table **as it was shown** (`digest`), with the lines the
  admin changed; a collision, two numbers on one place, is refused and nothing is written;
- `POST .../{id}/application` applies a validated table, in one transaction, only against the
  Edition it was made for. Nothing is downloaded again and no file is written for a number the
  library holds: the next follow cycle asks for the numbers it lacks and for nothing else.

A table is reached only through the Edition it was made for: the id of another Edition's table
is a table that does not exist. Every step writes one audit line (counts and ids only).
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.ops import OpsServices

__all__ = ["build_source_migration_router"]

_MIGRATE = reserved_action(RESERVED_ACTIONS["source.migrate"], audit=False)
Migrator = Annotated[Principal, Depends(_MIGRATE)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Ulid = Annotated[str, StringConstraints(min_length=26, max_length=26)]
Number = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposeIn(_Strict):
    source_key: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    remote_id: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    replaces_binding_id: Ulid | None = None


class OverrideIn(_Strict):
    number: Annotated[str, StringConstraints(max_length=64)] = ""  # as the new source writes it
    target: Number  # the number it takes in the library's own numbering
    # a line without a number is named by its `place` (as the table gives it), and its target is
    # the id of the Unit of the library it is paired with: a rank says nothing across two sources
    place: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None


class ValidateIn(_Strict):
    digest: Digest
    overrides: list[OverrideIn] = Field(default_factory=list, max_length=2000)


class ApplyIn(_Strict):
    digest: Digest


class MigrationRowOut(BaseModel):
    number: str
    place: str
    source_order: int
    target: str
    target_order: int
    unit_id: str | None
    basis: str  # same_number | offset | manual | new


class MigrationOut(BaseModel):
    id: str
    edition_id: str
    state: str  # proposed | validated | applied
    digest: str
    offset: int
    held_count: int
    unmatched_units: list[str]
    to_fetch: int  # the numbers the library lacks: the only ones the new source is asked for
    binding_id: str | None
    rows: list[MigrationRowOut]
    already: bool | None = None  # an application replayed: nothing was done now


def build_source_migration_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/editions/{edition_id}/source-migrations", tags=["sources"])

    @router.post("", status_code=201, response_model=MigrationOut)
    def source_migration_propose(
        edition_id: str, body: ProposeIn, principal: Migrator, response: Response
    ) -> MigrationOut:
        with refusals():
            done = get_ops().series_migration.propose(
                edition_id, source_key=body.source_key, remote_id=body.remote_id,
                replaces=body.replaces_binding_id, principal=principal,
            )  # fmt: skip
        return MigrationOut(**done)

    @router.get("/{migration_id}", response_model=MigrationOut)
    def source_migration_read(
        edition_id: str, migration_id: str, principal: Migrator
    ) -> MigrationOut:
        with refusals():
            done = get_ops().series_migration.get(edition_id, migration_id, principal=principal)
        return MigrationOut(**done)

    @router.post("/{migration_id}/validation", response_model=MigrationOut)
    def source_migration_validate(
        edition_id: str, migration_id: str, body: ValidateIn, principal: Migrator
    ) -> MigrationOut:
        with refusals():
            done = get_ops().series_migration.validate(
                edition_id, migration_id, digest=body.digest,
                overrides=[(o.number, o.target, o.place) for o in body.overrides],
                principal=principal,
            )  # fmt: skip
        return MigrationOut(**done)

    @router.post("/{migration_id}/application", response_model=MigrationOut)
    def source_migration_apply(
        edition_id: str, migration_id: str, body: ApplyIn, principal: Migrator
    ) -> MigrationOut:
        with refusals():
            done = get_ops().series_migration.apply(
                edition_id, migration_id, digest=body.digest, principal=principal
            )
        return MigrationOut(**done)

    return router
