"""`source migrate-ids` (EF-SRC-022, EF-BIB-054 (4)): the bulk migration of the identity of a
source's series after its id scheme changed, previewed before it is applied.

A reserved action (`source.migrate_ids`): the host CLI or a fresh admin session, never a key, and
one door per interface (the CLI command, `POST /api/v1/sources/{source_key}/migrate-ids`). The
preview is reserved as well, because it opens the tombstones to say which series they hold back.
**The preview writes nothing**: not a row, not a file, not an event, not a token. The migration acts
only for the fingerprint of a plan the admin saw, made again at that moment under the write lock;
its audit line is written in the transaction that moves the rows and holds the migration's own id,
a fingerprint and counts, never a reference or a source key.

The tombstone check runs on a service of its own, built over the registry of normalizers and
replayed, and it fails closed: with no library or no keyring there is no check, so no migration.
A series a tombstone holds is reported as blocked and only then is `tombstone.migration_hit` told
(the scope only), once the lock is let go.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.acquisition.application.alias_migration import (
    AliasPlan,
    Family,
    MigrateRef,
    Outcome,
    apply_alias_migration,
    plan_alias_migration,
)
from shelfhold.catalog.public import default_registry
from shelfhold.composition_errors import KeyringUnavailableError, NoLibraryError
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity
from shelfhold.library.public import SeriesBindingRef, TombstoneService, normalizers_of
from shelfhold.ops.errors import (
    CodedRefusal,
    ExitUnavailableError,
    InvalidChoiceError,
    StalePreviewError,
)

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["SourceMigrationOps"]

MAX_SCHEME_CHARS = 64


class SourceMigrationOps:
    def __init__(self, comp: Composition, ref_migrator: MigrateRef | None = None) -> None:
        self._comp = comp
        self._ref_migrator = ref_migrator  # a test hands one in; else the loaded plugin's

    def migrate_ids(
        self,
        source_key: str,
        *,
        from_scheme: str,
        to_scheme: str,
        confirm: str | None,
        principal: Principal,
    ) -> dict[str, Any]:
        """The plan for `source_key`; applied too when `confirm` is the fingerprint of that plan."""
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["source.migrate_ids"], now=self._comp.clock.now(),
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the door decided first; this is the last gate
            raise CodedRefusal("forbidden", type(verdict).__name__)
        self._check_schemes(source_key, from_scheme, to_scheme)
        service = self._service()
        migrate_ref = self._ref_migrator or self._plugin_migrator()
        screen = self._screen(service)

        def make_plan(connection: Connection) -> AliasPlan:
            return plan_alias_migration(
                connection, source_key=source_key, from_scheme=from_scheme, to_scheme=to_scheme,
                migrate_ref=migrate_ref, screen=screen, erased_refs=service.erased_series_refs,
            )  # fmt: skip

        if confirm is None:  # a preview: a read, on the tombstones' index, that writes nothing
            with self._comp.engine.connect() as connection:
                plan = make_plan(connection)
            return self._report(plan, None)
        # The plan is made again, the tombstones asked again and the rows moved under one write
        # lock: nothing can change between what is checked and what is written.
        with self._comp.engine.connect() as connection:
            connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
            with connection.begin():
                plan = make_plan(connection)
                if not hmac.compare_digest(confirm.strip().encode(), plan.fingerprint.encode()):
                    raise StalePreviewError(
                        "the plan is not the one that was previewed: look again"
                    )
                applied = apply_alias_migration(connection, plan, audit=self._audit(principal))
        if plan.of(Outcome.BLOCKED):  # told after the lock: the bus writes in its own transaction
            service.announce_hits(["series_binding"])
        return self._report(plan, applied)

    @staticmethod
    def _check_schemes(source_key: str, from_scheme: str, to_scheme: str) -> None:
        for value in (source_key, from_scheme, to_scheme):
            if not value or len(value) > MAX_SCHEME_CHARS * 4:
                raise InvalidChoiceError("a source and two id schemes are needed")
        if from_scheme == to_scheme or max(len(from_scheme), len(to_scheme)) > MAX_SCHEME_CHARS:
            raise InvalidChoiceError("the two id schemes must differ")

    def _service(self) -> TombstoneService:
        try:
            fsops, loaded, _cached = self._comp.read_context()
            return self._comp.fresh_tombstones(fsops, loaded, normalizers_of(default_registry()))
        except (NoLibraryError, KeyringUnavailableError) as error:
            raise ExitUnavailableError(
                "the tombstones cannot be checked (no library or no keyring): nothing is migrated"
            ) from error

    @staticmethod
    def _screen(service: TombstoneService) -> Callable[[Sequence[tuple[str, Family]]], set[str]]:
        def screen(rows: Sequence[tuple[str, Family]]) -> set[str]:
            return service.screen_refs(
                [(row_id, tuple(SeriesBindingRef(*member) for member in family))
                 for row_id, family in rows],
                announce=False,
            )  # fmt: skip

        return screen

    def _plugin_migrator(self) -> MigrateRef | None:
        """`migrate_ref` of the loaded plugin; the plugin is untrusted: a refusal, an error or a
        malformed answer says nothing, and what it says is screened like any alias."""
        registry = self._comp.plugins()
        if registry is None:
            return None

        def migrate(plugin_id: str, source_key: str, remote_id: str) -> tuple[str, str] | None:
            record = registry.get(plugin_id)
            ask = getattr(getattr(record, "instance", None), "migrate_ref", None)
            if ask is None:
                return None
            try:
                answer = ask(source_key, remote_id)
            except Exception:  # a plugin's own failure is its own, never ours
                return None
            if (
                isinstance(answer, tuple) and len(answer) == 2
                and all(isinstance(part, str) and part for part in answer)
            ):  # fmt: skip
                return answer[0], answer[1]
            return None

        return migrate

    @staticmethod
    def _audit(principal: Principal) -> Callable[[Connection, AliasPlan, str, int, int], None]:
        def write(
            connection: Connection, plan: AliasPlan, migration_id: str, moved: int, held: int
        ) -> None:
            connection.execute(
                sa.insert(identity.audit).values(
                    id=new_ulid(), actor_id=principal.actor_id, action="source.migrate_ids",
                    target=f"mig:{migration_id};fp:{plan.fingerprint[:16]};rows:{moved};held:{held}",
                    session_id=principal.session_id, auth_time=principal.auth_time,
                )
            )  # fmt: skip

        return write

    @staticmethod
    def _report(plan: AliasPlan, applied: int | None) -> dict[str, Any]:
        def shown(ref: tuple[str, str] | None) -> str:
            if ref is None:
                return ""
            return ref[1] if ref[0] == plan.source_key else f"{ref[0]}/{ref[1]}"

        def rows(outcome: Outcome) -> list[dict[str, str]]:
            return [
                {
                    "id": r.id,
                    "old": r.old[1],
                    "new": shown(r.new) if r.outcome is Outcome.MIGRATE else "",
                    "reason": r.reason,
                }
                for r in plan.of(outcome)
            ]  # a blocked row never names where it would have gone

        return {
            "source_key": plan.source_key,
            "from_scheme": plan.from_scheme,
            "to_scheme": plan.to_scheme,
            "migrate": rows(Outcome.MIGRATE),
            "blocked": rows(Outcome.BLOCKED),
            "unresolved": rows(Outcome.UNRESOLVED),
            "fingerprint": plan.fingerprint,
            "applied": applied,
        }
