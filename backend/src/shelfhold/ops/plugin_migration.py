"""`plugin migrate-to` (EF-BIB-054 (4), EF-SRC-030): the sources of a plugin move to the
replacement its index named.

A reserved action (`plugin.migrate_to`): the host CLI or a fresh admin session, never a key.
The replacement is never installed by the index's word alone (another key is a new install,
with its own checks) — this is the admin's own act on what the old plugin owns. Every living
line is asked about as a family of references: its new form under the replacing plugin and its
old form (the inverse alias), because a line that would meet a tombstone, whichever form it
got there by, stays where it is — nothing is recreated for an erased series. A blocked line is
reported, and only then is `tombstone.migration_hit` told (the scope only), once the lock is
let go.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from shelfhold.catalog.public import default_registry
from shelfhold.composition_errors import KeyringUnavailableError, NoLibraryError
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, identity
from shelfhold.library.public import SeriesBindingRef, TombstoneService, normalizers_of
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["PluginMigrationOps"]


class PluginMigrationOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def migrate_to(self, *, key: str, replacement: str, principal: Principal) -> dict[str, Any]:
        """The sources of `key` move to `replacement`; the ones a tombstone holds stay, with
        the reason. The audit line and the moved rows are one transaction."""
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["plugin.migrate_to"], now=self._comp.clock.now(),
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the door decided first; this is the last gate
            raise CodedRefusal("forbidden", type(verdict).__name__)
        if key == replacement:
            raise CodedRefusal("same_key", "same_key: a plugin is not its own replacement")
        service = self._service()
        with self._comp.engine.connect() as connection:
            items = connection.execute(
                sa.select(
                    acquisition.source_item.c.id,
                    acquisition.source_item.c.source_key,
                    acquisition.source_item.c.remote_id,
                ).where(acquisition.source_item.c.plugin_id == key)
            ).all()
        if not items:
            raise CodedRefusal("unknown", f"unknown plugin: no source of {key!r} is in the library")
        # the family of each line: its new form under the replacing plugin, and its old form
        # (the inverse alias) — a tombstone met by either one holds the line where it is
        families = [
            (
                row.id,
                (
                    SeriesBindingRef(replacement, row.source_key, row.remote_id),
                    SeriesBindingRef(key, row.source_key, row.remote_id),
                ),
            )
            for row in items
        ]
        blocked = service.screen_refs(families, announce=False)
        moved = [row for row in items if row.id not in blocked]
        with write_transaction(self._comp.engine) as connection:
            for row in moved:
                connection.execute(
                    sa.update(acquisition.source_item)
                    .where(acquisition.source_item.c.id == row.id)
                    .values(plugin_id=replacement)
                )
            connection.execute(
                sa.insert(identity.audit).values(
                    id=new_ulid(), actor_id=principal.actor_id, action="plugin.migrated_to",
                    target=f"{key}->{replacement};rows:{len(moved)};blocked:{len(blocked)}",
                    session_id=principal.session_id, auth_time=principal.auth_time,
                )
            )  # fmt: skip
        if blocked:  # after the lock is let go, like every migration that tells its hits
            service.announce_hits(["series_binding"])
        return {"moved": len(moved), "blocked": len(blocked)}

    def _service(self) -> TombstoneService:
        try:
            fsops, loaded, _cached = self._comp.read_context()
            return self._comp.fresh_tombstones(fsops, loaded, normalizers_of(default_registry()))
        except (NoLibraryError, KeyringUnavailableError) as error:
            raise ExitUnavailableError(
                "the tombstones cannot be checked (no library or no keyring): nothing is migrated"
            ) from error
