"""The migration of a series to another source, as the interfaces reach it (EF-BIB-014, M4 L27).

The door is the HTTP route (`interfaces/http_api/source_migration.py`), the reserved action
`source.migrate`: a fresh admin session, never a key. This layer reads what the new source lists
(the connected service, once, outside any transaction), then runs each step in its own
`BEGIN IMMEDIATE` transaction: what is checked is what is written, and a cut leaves the old state.
A refusal of the service is a code of the catalog, and says nothing of which Editions or tables
exist beyond the one that was asked for.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection

from shelfhold.acquisition.public import (
    CollisionError,
    InvalidMigrationError,
    MigrationNotAllowedError,
    MigrationOutcome,
    MigrationProposal,
    NotValidatedError,
    Override,
    SourceMigrations,
    StaleMigrationError,
    UnknownMigrationError,
)
from shelfhold.catalog.public import ListedUnit
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.db import write_transaction
from shelfhold.ops.errors import (
    CodedRefusal,
    ExitUnavailableError,
    OpsError,
    UnknownSubjectError,
)

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["SeriesMigrationOps"]

SERVICE_ID = "suwayomi"  # the one connected service whose series a migration moves


def _refusal(error: Exception) -> OpsError | None:
    if isinstance(error, MigrationNotAllowedError):  # the door decided first: the last gate
        return CodedRefusal("forbidden", type(error).__name__)
    if isinstance(error, UnknownMigrationError):
        return UnknownSubjectError("unknown table")
    if isinstance(error, CollisionError):
        return CodedRefusal("source_migration.collision", type(error).__name__)
    if isinstance(error, NotValidatedError):
        return CodedRefusal("source_migration.not_validated", type(error).__name__)
    if isinstance(error, StaleMigrationError):
        return CodedRefusal("source_migration.stale", type(error).__name__)
    if isinstance(error, InvalidMigrationError | LookupError | ValueError):
        return CodedRefusal("source_migration.invalid", type(error).__name__)
    return None


def _refused[T](step: Callable[[], T]) -> T:
    try:
        return step()
    except OpsError:
        raise
    except Exception as error:
        refusal = _refusal(error)
        if refusal is None:
            raise
        raise refusal from None


def shown(proposal: MigrationProposal, *, binding_id: str | None = None) -> dict[str, Any]:
    """The table as the interfaces show it: numbers and ids, never a title or a source's name."""
    return {
        "id": proposal.id,
        "edition_id": proposal.edition_id,
        "state": proposal.state,
        "digest": proposal.digest,
        "offset": proposal.offset,
        "held_count": proposal.held_count,
        "unmatched_units": list(proposal.unmatched_units),
        "to_fetch": len(proposal.to_fetch),
        "binding_id": binding_id or proposal.binding_id,
        "rows": [
            {
                "number": row.number if row.number is not None else "",
                "place": row.place,
                "source_order": row.source_order,
                "target": row.target if row.target is not None else "",
                "target_order": row.target_order,
                "unit_id": row.unit_id,
                "basis": row.basis,
            }
            for row in proposal.rows
        ],
    }


class SeriesMigrationOps:
    def __init__(
        self,
        comp: Composition,
        listing: Callable[[str, str], tuple[Any, Sequence[ListedUnit]]] | None = None,
    ) -> None:
        self._comp = comp
        self._listing = listing or self._service_listing  # a test hands one in

    def propose(
        self, edition_id: str, *, source_key: str, remote_id: str, replaces: str | None,
        principal: Principal,
    ) -> dict[str, Any]:  # fmt: skip
        """The correspondence for what the new source lists now: nothing else is written."""
        ref, listed = _refused(lambda: self._listing(source_key, remote_id))
        if ref is None:
            raise CodedRefusal(
                "source_migration.invalid", "the service does not follow that series"
            )

        def propose(connection: Connection) -> MigrationProposal:
            return SourceMigrations().propose(
                connection, edition_id, plugin_id=SERVICE_ID, source_key=ref.source_key,
                remote_id=ref.remote_id, listed=listed, replaces_binding_id=replaces,
                memo=getattr(ref, "memo", None), memo_version=getattr(ref, "memo_version", None),
                principal=principal, now=self._comp.clock.now(),
            )  # fmt: skip

        return shown(_refused(lambda: self._run(propose)))

    def get(self, edition_id: str, migration_id: str, *, principal: Principal) -> dict[str, Any]:
        def read(connection: Connection) -> MigrationProposal:
            return SourceMigrations().get(connection, edition_id, migration_id, principal=principal)

        return shown(_refused(lambda: self._run(read, write=False)))

    def validate(
        self, edition_id: str, migration_id: str, *, digest: str,
        overrides: Sequence[tuple[str, str]], principal: Principal,
    ) -> dict[str, Any]:  # fmt: skip
        def validate(connection: Connection) -> MigrationProposal:
            return SourceMigrations().validate(
                connection, edition_id, migration_id, digest=digest,
                overrides=[Override(number, target) for number, target in overrides],
                principal=principal, now=self._comp.clock.now(),
            )  # fmt: skip

        return shown(_refused(lambda: self._run(validate)))

    def apply(
        self, edition_id: str, migration_id: str, *, digest: str, principal: Principal
    ) -> dict[str, Any]:
        def apply(connection: Connection) -> MigrationOutcome:
            return SourceMigrations().apply(
                connection, edition_id, migration_id, digest=digest, principal=principal,
                now=self._comp.clock.now(),
            )  # fmt: skip

        done = _refused(lambda: self._run(apply))
        return shown(done.proposal, binding_id=done.binding_id) | {"already": done.already}

    # -- plumbing ------------------------------------------------------------------------------
    def _run[T](self, work: Callable[[Connection], T], *, write: bool = True) -> T:
        if write:
            with write_transaction(self._comp.engine) as connection:
                return work(connection)
        with self._comp.engine.connect() as connection:
            return work(connection)

    def _service_listing(self, source_key: str, remote_id: str) -> tuple[Any, Sequence[ListedUnit]]:
        """The series the connected service follows under this source, with its releases."""
        link = self._comp.suwayomi()
        if link is None:
            raise ExitUnavailableError("no Suwayomi is configured: `shelfhold source add suwayomi`")

        async def read() -> tuple[Any, list[ListedUnit]]:
            try:
                library = await link.service.followed_series(link.fetcher)
                ref = next(
                    (r for r in library if (r.source_key, r.remote_id) == (source_key, remote_id)),
                    None,
                )
                if ref is None:
                    return None, []
                units = await link.service.list_units(link.fetcher, ref)
                return ref, [ListedUnit(u.number, u.source_order) for u in units]
            finally:
                await link.guarded.aclose()

        return asyncio.run(read())
