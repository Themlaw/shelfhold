"""``shelfhold shelf create``: the link to a Kavita library, audited first (EF-EXP-002, EF-EXP-055).

The spec says *link*, and the audit comes first, so the order here is:

1. read the libraries of the server; one that already covers the shelf's folder is **linked**,
   after the read-only audit has named nobody who must not read it (children): else refused,
   nothing written in Kavita;
2. none covers the folder: one is **created** (only then: the headless install of EF-INT-024
   goes from an empty Kavita to the first chapter without another step). A library that does
   not exist yet cannot be read by any account, so the audit before it is vacuous; the whole
   audit runs again right after the link, and a restricted account pauses the shelf;
3. the link is stored and the audit is replayed (stored for the daily check).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.export.application.snapshot import load_context
from shelfhold.export.domain.kavita_instance import covering_libraries
from shelfhold.export.targets.kavita.adopt import AdoptionRefused, NoFolderError, ensure_link
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.reinstall import library_type_of
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import export

__all__ = ["ShelfLinker"]


class ShelfLinker:
    def __init__(self, engine: Engine, clock: Clock, auditor: AccessAuditor) -> None:
        self._engine = engine
        self._clock = clock
        self._auditor = auditor

    async def link(self, client: KavitaClient, destination_id: str, *, library_name: str) -> str:
        """Returns the reference of the library the destination is now linked to."""
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
            library_type = connection_library_type(connection, destination_id)
        if ctx.root_local is None:
            raise NoFolderError(destination_id)
        remote_root = ctx.remote_folder(ctx.root_local)
        covering = covering_libraries(await client.list_libraries(), remote_root)
        if covering:
            ref = str(covering[0])
            refused = await self._auditor.refused_names(
                client, shelf_ceiling=ctx.max_age_rating, remote_library_ref=ref,
                remote_root=remote_root,
            )  # fmt: skip
            if refused:
                raise AdoptionRefused(refused)
        else:
            created = await client.create_library(
                library_name, library_type_of(library_type), [remote_root]
            )
            ref = str(created["id"])
        ensure_link(self._engine, self._clock, ctx, ref)
        await self._auditor.audit(client, ctx.target_id)
        return ref


def connection_library_type(connection: Connection, destination_id: str) -> str | None:
    return connection.execute(
        sa.select(export.shelf_destination.c.kavita_library_type).where(
            export.shelf_destination.c.id == destination_id
        )
    ).scalar()
