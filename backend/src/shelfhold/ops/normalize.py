"""`normalize migrate` (ENF-OPS-021, EF-BIB-054 (4)): move rows to the current version of a
normalizer, after a preview, without ever re-acquiring what was purged.

A reserved action (`normalize.migrate`): the host CLI or a fresh admin session, never a key. It
changes the identity keys of live rows, so it asks for a fresh authentication like the other
irreversible actions, and the preview is reserved as well: it decrypts the tombstones to say which
rows they block. The preview writes nothing. The migration acts only for the fingerprint of a plan
the admin saw, recomputed at that moment; its audit entry is written in the same transaction as
the rows it moved.

The tombstone check runs on a service of its own, built over the very registry the plan is made
with and replayed (its index is rebuilt under those versions), and it fails closed: with no
library or no keyring there is no check, so there is no migration.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.application.normalize_migrate import (
    MIGRATABLE,
    Candidate,
    MigrationPlan,
    Outcome,
    Screen,
    apply_number_migration,
    plan_number_migration,
)
from shelfhold.catalog.public import NormalizerRegistry, default_registry
from shelfhold.composition_errors import KeyringUnavailableError, NoLibraryError
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, identity
from shelfhold.library.public import EDITION_KEY_COLUMNS, PlaceRef, TombstoneService, normalizers_of
from shelfhold.ops.errors import (
    CodedRefusal,
    ExitUnavailableError,
    InvalidChoiceError,
    StalePreviewError,
)

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["NormalizeOps"]

_CHUNK = 400


class NormalizeOps:
    def __init__(
        self,
        comp: Composition,
        registry: Callable[[], NormalizerRegistry] = default_registry,
    ) -> None:
        self._comp = comp
        self._registry = registry  # the versions in the code; a test hands another one in

    def migrate(self, name: str, *, confirm: str | None, principal: Principal) -> dict[str, Any]:
        """The plan for `name`; applied too when `confirm` is the fingerprint of that plan."""
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["normalize.migrate"], now=self._comp.clock.now(),
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the socket decided first; this one is the last gate
            raise CodedRefusal("forbidden", type(verdict).__name__)
        if name not in MIGRATABLE:
            raise InvalidChoiceError(f"choose one of: {', '.join(MIGRATABLE)}")
        plan = self._plan()
        applied: int | None = None
        if confirm is not None:
            if not hmac.compare_digest(confirm.strip().encode(), plan.fingerprint.encode()):
                raise StalePreviewError("the plan is not the one that was previewed: look again")
            with self._comp.engine.begin() as connection:
                applied = apply_number_migration(connection, plan, audit=self._audit(principal))
        return self._report(plan, applied)

    def _plan(self) -> MigrationPlan:
        registry = self._registry()
        try:
            fsops, loaded, _cached = self._comp.read_context()
            service = self._comp.fresh_tombstones(fsops, loaded, normalizers_of(registry))
        except (NoLibraryError, KeyringUnavailableError) as error:
            raise ExitUnavailableError(
                "the tombstones cannot be checked (no library or no keyring): nothing is migrated"
            ) from error
        with self._comp.engine.connect() as connection:
            return plan_number_migration(connection, registry, self._screen(service))

    def _screen(self, service: TombstoneService) -> Screen:
        def screen(candidates: Sequence[Candidate]) -> set[str]:
            if not candidates:
                return set()
            with self._comp.engine.connect() as connection:
                keys = _edition_keys(connection, {c.edition_id for c in candidates})
            return service.screen_migration(
                [
                    (
                        c.row_id,
                        PlaceRef.of(
                            keys[c.edition_id], track=c.track, number_segment=c.number_segment,
                            number_raw=c.number_raw, part=c.part,
                        ),
                    )
                    for c in candidates
                ]
            )  # fmt: skip

        return screen

    @staticmethod
    def _audit(principal: Principal) -> Callable[[Connection, MigrationPlan, int], None]:
        def write(connection: Connection, plan: MigrationPlan, moved: int) -> None:
            connection.execute(
                sa.insert(identity.audit).values(
                    id=new_ulid(), actor_id=principal.actor_id, action="normalize.migrate",
                    target=str(plan.target), session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )  # fmt: skip

        return write

    @staticmethod
    def _report(plan: MigrationPlan, applied: int | None) -> dict[str, Any]:
        def rows(outcome: Outcome) -> list[dict[str, str]]:
            return [
                {"id": r.row_id, "old": r.old, "new": r.new, "reason": r.reason}
                for r in plan.of(outcome)
            ]

        return {
            "name": plan.name,
            "target": str(plan.target),
            "migrate": rows(Outcome.MIGRATE),
            "merge": rows(Outcome.MERGE),
            "blocked": rows(Outcome.BLOCKED),
            "unresolved": rows(Outcome.UNRESOLVED),
            "collisions": [list(group) for group in plan.collisions],
            "fingerprint": plan.fingerprint,
            "applied": applied,
        }


def _edition_keys(connection: Connection, edition_ids: set[str]) -> dict[str, dict[str, str]]:
    edition = catalog.edition
    wanted = sorted(edition_ids)
    found: dict[str, dict[str, str]] = {}
    for start in range(0, len(wanted), _CHUNK):
        for row in connection.execute(
            sa.select(edition.c.id, *(edition.c[k] for k in EDITION_KEY_COLUMNS)).where(
                edition.c.id.in_(wanted[start : start + _CHUNK])
            )
        ):
            found[row.id] = {k: str(getattr(row, k)) for k in EDITION_KEY_COLUMNS}
    return found
