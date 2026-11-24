"""`plugin migrate-to` (EF-BIB-054 (4), EF-SRC-030): the sources of a plugin move to the
replacement its index named.

A reserved action (`plugin.migrate_to`): the host CLI or a fresh admin session, never a key.
The replacement is the key the old plugin's signed index named in `replaced_by` — the word the
install kept beside its pin, like `min_host` — and never a revoked key. A plugin no index ever
vouched for (nothing ever installed it) has no such word: only its own family, the same first
segment of its key (a re-keying of the same plugin), moves without one. The replacement is never
installed by the index's word alone (another key is a new install, with its own checks) — this
is the admin's own act on what the old plugin owns.

Every living line is asked about as a family of references: its new form under the replacing
plugin and its old form (the inverse alias), because a line that would meet a tombstone,
whichever form it got there by, stays where it is — nothing is recreated for an erased series.
The ask, the move and the audit line are one transaction under the write lock: nothing can
change between what is checked and what is written. A blocked line is reported, and only then
is `tombstone.migration_hit` told (the scope only), once the lock is let go. After the lock,
each tombstone of the old key and each form a tombstone held back is sealed under the replacing
key, like every identity migration (`_seal` of `source migrate-ids`): the replacement acquires
nothing the old plugin had lost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.public import default_registry
from shelfhold.composition_errors import KeyringUnavailableError, NoLibraryError
from shelfhold.export.domain.path_map import PathMode
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, identity
from shelfhold.library.public import (
    SeriesBindingRef,
    TombstoneRoot,
    TombstoneService,
    alias_family,
    normalizers_of,
    safe_fold,
)
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError
from shelfhold.plugin_host.index.service import RepoStore
from shelfhold.plugin_host.rpc.runner_wire import KEY

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["PluginMigrationOps"]


class PluginMigrationOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def migrate_to(self, *, key: str, replacement: str, principal: Principal) -> dict[str, Any]:
        """The sources of `key` move to `replacement`; the ones a tombstone holds stay, with
        the reason. The ask, the moved rows, the aliases that follow and the audit line are one
        transaction under the write lock."""
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["plugin.migrate_to"], now=self._comp.clock.now(),
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the door decided first; this is the last gate
            raise CodedRefusal("forbidden", type(verdict).__name__)
        if key == replacement:
            raise CodedRefusal("same_key", "same_key: a plugin is not its own replacement")
        if not KEY.fullmatch(replacement):
            raise CodedRefusal("not_named", f"not_named: {replacement!r} is not a plugin key")
        service = self._service()
        # The plan is made, the tombstones asked and the rows moved under one write lock: nothing
        # can change between what is checked and what is written.
        with self._comp.engine.connect() as connection:
            connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
            with connection.begin():
                items = connection.execute(
                    sa.select(
                        acquisition.source_item.c.id,
                        acquisition.source_item.c.source_key,
                        acquisition.source_item.c.remote_id,
                    ).where(acquisition.source_item.c.plugin_id == key)
                ).all()
                if not items:
                    raise CodedRefusal(
                        "unknown", f"unknown plugin: no source of {key!r} is in the library"
                    )
                self._check_replacement(connection, key, replacement)
                # the family of each line: its new form under the replacing plugin, and its old
                # form (the inverse alias) — a tombstone met by either one holds the line
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
                self._refuse_collisions(connection, key, replacement, moved)
                for row in moved:
                    connection.execute(
                        sa.update(acquisition.source_item)
                        .where(acquisition.source_item.c.id == row.id)
                        .values(plugin_id=replacement)
                    )
                self._move_aliases(connection, key, replacement, blocked)
                connection.execute(
                    sa.insert(identity.audit).values(
                        id=new_ulid(), actor_id=principal.actor_id, action="plugin.migrated_to",
                        target=f"{key}->{replacement};rows:{len(moved)};blocked:{len(blocked)}",
                        session_id=principal.session_id, auth_time=principal.auth_time,
                    )
                )  # fmt: skip
        # After the lock is let go (the tombstones write in their own transactions): the new
        # forms are sealed, and the hits told. A cut in between leaves the migration to be made
        # again: the forms still to seal are the same ones, so a rerun finishes the job.
        self._seal(service, key, replacement, items, blocked)
        if blocked:  # after the lock is let go, like every migration that tells its hits
            service.announce_hits(["series_binding"])
        return {"moved": len(moved), "blocked": len(blocked)}

    def _check_replacement(self, connection: Connection, key: str, replacement: str) -> None:
        """The replacement is the key the old plugin's signed index named in `replaced_by` —
        the word the install kept beside its pin — and never a revoked key. A plugin no index
        ever vouched for has no word: only its own family (the same first segment of its key, a
        re-keying of the same plugin) moves without one."""
        named = connection.execute(
            sa.select(acquisition.plugin_install.c.replaced_by).where(
                acquisition.plugin_install.c.plugin_id == key
            )
        ).first()
        revoked = RepoStore(self._comp.engine, now=self._comp.clock.now).revoked_plugin_keys()
        if replacement in revoked:
            raise CodedRefusal(
                "revoked", f"revoked: the key of {replacement!r} was revoked by an index"
            )  # fmt: skip
        if named is not None:
            if replacement != named.replaced_by:
                raise CodedRefusal(
                    "not_named",
                    f"not_named: no index named {replacement!r} as the replacement of {key!r}",
                )
            return
        if replacement.partition("-")[0] != key.partition("-")[0]:
            raise CodedRefusal(
                "not_named",
                f"not_named: no index named {replacement!r} as the replacement of {key!r}",
            )

    @staticmethod
    def _refuse_collisions(
        connection: Connection, key: str, replacement: str, moved: list[Any]
    ) -> None:
        """A reference the replacement already owns, or an alias it already keeps, is a clean
        refusal — never the database's own IntegrityError, which would say nothing."""
        if moved:
            taken = {
                (row.source_key, row.remote_id)
                for row in connection.execute(
                    sa.select(
                        acquisition.source_item.c.source_key, acquisition.source_item.c.remote_id
                    ).where(acquisition.source_item.c.plugin_id == replacement)
                )
            }
            clash = sorted(
                f"{source}:{remote}"
                for source, remote in ((row.source_key, row.remote_id) for row in moved)
                if (source, remote) in taken
            )
            if clash:
                raise CodedRefusal(
                    "collision", f"collision: the replacement already owns {', '.join(clash)}"
                )
        kept = {
            (row.old_source_key, row.old_remote_id)
            for row in connection.execute(
                sa.select(
                    acquisition.source_alias.c.old_source_key,
                    acquisition.source_alias.c.old_remote_id,
                ).where(acquisition.source_alias.c.plugin_id == replacement)
            )
        }
        moving = {
            (row.old_source_key, row.old_remote_id)
            for row in connection.execute(
                sa.select(
                    acquisition.source_alias.c.old_source_key,
                    acquisition.source_alias.c.old_remote_id,
                ).where(acquisition.source_alias.c.plugin_id == key)
            )
        }
        if kept & moving:
            raise CodedRefusal(
                "collision", "collision: the replacement already keeps one of the aliases"
            )

    @staticmethod
    def _move_aliases(
        connection: Connection, key: str, replacement: str, blocked: set[str]
    ) -> None:
        """The aliases of the plugin follow it to the replacing key — except the components a
        tombstone holds back: the lines that stayed keep the aliases that hold them, under the
        old key, or the block would leave with the alias."""
        alias = acquisition.source_alias
        edges = [
            (row.old_source_key, row.old_remote_id, row.new_source_key, row.new_remote_id)
            for row in connection.execute(
                sa.select(
                    alias.c.old_source_key, alias.c.old_remote_id,
                    alias.c.new_source_key, alias.c.new_remote_id,
                ).where(alias.c.plugin_id == key)
            )
        ]  # fmt: skip
        if not edges:
            return
        held = {
            (safe_fold(row.source_key), safe_fold(row.remote_id))
            for row in connection.execute(
                sa.select(
                    acquisition.source_item.c.source_key, acquisition.source_item.c.remote_id
                ).where(acquisition.source_item.c.id.in_(blocked))
            )
        }
        for old_key, old_id, _new_key, _new_id in edges:
            component = alias_family(edges, (old_key, old_id)) or ()
            if held & set(component):
                continue  # the component holds a blocked line back: it stays with it
            connection.execute(
                sa.update(alias)
                .where(
                    alias.c.plugin_id == key,
                    alias.c.old_source_key == old_key,
                    alias.c.old_remote_id == old_id,
                )
                .values(plugin_id=replacement)
            )

    def _seal(
        self,
        service: TombstoneService,
        key: str,
        replacement: str,
        items: list[Any],
        blocked: set[str],
    ) -> None:
        """One more tombstone of the same scope (`series_binding`), sealed like the others, on
        each new form of an erased series of the old key and of a line a tombstone held back:
        the replacement acquires nothing the old plugin had lost. The origins are the tokens of
        the tombstones each new form comes from: lifting one of them lifts this one. The reads
        come from the migration's own replayed service (the long-lived one may not have seen a
        purge another process laid); the record goes through the write context, like every
        tombstone the host writes."""
        with self._comp.write_context() as w:
            sources = service.erased_series_sources(key)
            erased = {
                (source, remote)
                for source in sources
                for remote in service.erased_series_refs(key, source)
            }
            forms: dict[tuple[str, str, str], list[str]] = {
                (replacement, source, remote): service.origin_tokens(
                    SeriesBindingRef(key, source, remote)
                )
                for source, remote in erased
            }
            edges = self._alias_edges(key)
            for row in items:
                if row.id not in blocked:
                    continue
                family = set(alias_family(edges, (row.source_key, row.remote_id)) or ())
                forms[(replacement, row.source_key, row.remote_id)] = [
                    token
                    for source, remote in sorted(erased)
                    if (safe_fold(source), safe_fold(remote)) in family
                    for token in service.origin_tokens(SeriesBindingRef(key, source, remote))
                ]
            if not forms:
                return
            roots = [
                TombstoneRoot(
                    r.root_id, r.path, w.fsops.root_state(r.path) is RootState.OK, r.predecessors
                )
                for r in w.loaded.roots
                if r.mode is PathMode.RW
            ]
            for form, origins in forms.items():
                values = SeriesBindingRef(*form)
                if not service.has_values(values):
                    w.tombstones.record(values, roots, reason="new_form", origins=origins)

    def _alias_edges(self, key: str) -> list[tuple[str, str, str, str]]:
        alias = acquisition.source_alias
        with self._comp.engine.connect() as connection:
            return [
                (row.old_source_key, row.old_remote_id, row.new_source_key, row.new_remote_id)
                for row in connection.execute(
                    sa.select(
                        alias.c.old_source_key, alias.c.old_remote_id,
                        alias.c.new_source_key, alias.c.new_remote_id,
                    ).where(alias.c.plugin_id == key)
                )
            ]  # fmt: skip

    def _service(self) -> TombstoneService:
        try:
            fsops, loaded, _cached = self._comp.read_context()
            return self._comp.fresh_tombstones(fsops, loaded, normalizers_of(default_registry()))
        except (NoLibraryError, KeyringUnavailableError) as error:
            raise ExitUnavailableError(
                "the tombstones cannot be checked (no library or no keyring): nothing is migrated"
            ) from error
