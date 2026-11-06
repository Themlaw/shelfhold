"""The lost-keyring exit over the tombstone service (EF-BIB-053, R62).

`waiting` is cheap (the registry of the database against the keyring, no replay of the journals):
it is read by `/healthz`. The waiver itself builds the service over the mounted roots and runs the
reserved action there, where its rule and its audit live.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.composition import open_fsops
from shelfhold.config import Settings
from shelfhold.export.application.roots import load_roots
from shelfhold.export.domain.path_map import PathMap, PathMode
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import coherence, platform
from shelfhold.infrastructure.secrets import diagnostics
from shelfhold.infrastructure.secrets.errors import SecretsError
from shelfhold.integrations.apprise import operating_bus
from shelfhold.library.public import TombstoneRoot, TombstoneService
from shelfhold.ops.ports import WaitingItem

__all__ = ["TombstoneKeyringExit"]


class TombstoneKeyringExit:
    def __init__(self, settings: Settings, engine: Engine, clock: Clock) -> None:
        self._settings = settings
        self._engine = engine
        self._clock = clock

    def waiting(self) -> list[WaitingItem]:
        try:
            keyring = diagnostics.boot_check(self._settings, create=False, announce=False)
        except SecretsError:
            return []  # no readable keyring: the boot check says so, this is not a key to waive
        kid, lost = coherence.tombstone_kid, coherence.tombstone_lost_kid
        with self._engine.connect() as connection:
            known = set(connection.execute(sa.select(kid.c.kid)).scalars())
            waived = set(connection.execute(sa.select(lost.c.kid)).scalars())
        owed = coherence.tombstone_pending_root
        with self._engine.connect() as connection:
            owing = sorted(set(connection.execute(sa.select(owed.c.root_id)).scalars()))
        return [
            WaitingItem("tombstone_key_unknown", "tombstones", k, "kid not in the keyring")
            for k in sorted(known - waived)
            if keyring.find(k) is None
        ] + [
            WaitingItem("tombstone_lines_owed", "tombstones", root_id, "lines owed to a root")
            for root_id in owing
        ]

    def _service(self) -> tuple[TombstoneService, list[TombstoneRoot]]:
        """The service over the mounted roots. A root counts as mounted when its sentinel can
        be judged, `restored_behind` included (the exits below are how that state is left)."""
        keyring = diagnostics.boot_check(self._settings, create=False, announce=False)
        with self._engine.connect() as connection:
            paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
            loaded = load_roots(connection, PathMap.same_path(paths))
        fsops = open_fsops(loaded)
        service = TombstoneService(self._engine, keyring, fsops, operating_bus(self._clock))
        roots = [
            TombstoneRoot(
                r.root_id, r.path,
                mounted=fsops.root_state(r.path) in (RootState.OK, RootState.RESTORED_BEHIND),
                predecessor_root_ids=r.predecessors,
            )
            for r in loaded.roots
            if r.mode is PathMode.RW
        ]  # fmt: skip
        return service, roots

    def set_aside_journal(
        self, *, principal: Principal, root_id: str | None = None
    ) -> dict[str, str]:
        service, roots = self._service()
        return service.set_aside_journal(
            roots, principal=principal, now=self._clock.now(), root_id=root_id
        )

    def flush_owed(self) -> list[str]:
        service, roots = self._service()
        for root in roots:
            service.flush_pending(root)
        return service.owed_roots()

    def accept_lost_keyring(self, *, confirmation: str, principal: Principal) -> list[str]:
        service, roots = self._service()
        return service.accept_lost_keyring(
            roots, confirmation=confirmation, principal=principal, now=self._clock.now()
        )
