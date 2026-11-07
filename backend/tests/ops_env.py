"""Harness of the operating-surface tests (L36): the services over an export environment, with the
ports of the other lots replaced by recorders (nothing leaves the test)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from shelfhold.events.bus import EventBus
from shelfhold.export.application.target_health import TargetHealth
from shelfhold.export.application.target_state import save_health
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.reinstall import InstanceService
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, platform
from shelfhold.ops import KavitaTargetOps, OpsServices, WaitingItem
from tests.export_env import ExportEnv
from tests.kavita_export_env import Wired

LOCAL_CLI = Principal(
    user_id="1000", username="uid:1000", role="admin", credential=Credential.LOCAL_CLI
)
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@dataclass
class FakeRestore:
    """A spy on the `RestoreExit` port, for the tests of the channels (which credential reaches
    it). It is not how a state is produced: the scenarios of `test_ops_exits` use the restore
    guard, the sentinels and `reconcile --adopt-after-restore`."""

    behind: bool = True
    calls: list[bool] = field(default_factory=list)

    def waiting(self) -> list[WaitingItem]:
        if not self.behind:
            return []
        return [WaitingItem("restored_behind", "instance", "instance", "database older than disk")]

    def adopt_after_restore(self, *, confirm_deletion: bool, principal: Principal) -> None:
        self.calls.append(confirm_deletion)
        self.behind = False


@dataclass
class FakeKeyring:
    """A spy on the `KeyringExit` port, for the tests of the channels. `tombstone_key_unknown` is
    produced by a real purge and a keyring that lost its key in `test_ops_exits`; the lines owed
    to a root that was away are the one state still listed by this spy."""

    kids: list[str] = field(default_factory=lambda: ["kid-lost"])
    waived: list[tuple[str, str]] = field(default_factory=list)
    owed: list[str] = field(default_factory=list)  # roots that owe lines
    aside: list[tuple[str, str | None]] = field(default_factory=list)
    imports: list[tuple[str, str, str]] = field(default_factory=list)  # (passphrase_file, in, who)

    def waiting(self) -> list[WaitingItem]:
        return [
            WaitingItem("tombstone_key_unknown", "tombstones", kid, "kid not in the keyring")
            for kid in self.kids
        ] + [
            WaitingItem("tombstone_lines_owed", "tombstones", root, "lines owed to a root")
            for root in self.owed
        ]

    def set_aside_journal(
        self, *, principal: Principal, root_id: str | None = None
    ) -> dict[str, str]:
        self.aside.append((principal.credential.value, root_id))
        return {"root": "tombstones.aside-20260101T000000Z"}

    def flush_owed(self) -> list[str]:
        self.owed = []
        return []

    def import_keyring(
        self, *, passphrase_file: str, in_file: str, principal: Principal,
        replay: bool = True, confined: bool = False,
    ) -> dict[str, Any]:  # fmt: skip
        self.imports.append((passphrase_file, in_file, principal.credential.value))
        self.kids = []
        return {
            "added": ["k-0123456789abcdef"],
            "present": 1,
            "replayed": 0,
            "deferred": not replay,
        }

    def accept_lost_keyring(
        self, *, confirmation: str, principal: Principal, frozen: bool = False
    ) -> list[str]:
        self.waived.append((confirmation, principal.credential.value))
        gone, self.kids = self.kids, []
        return gone


def make_ops(env: ExportEnv, wired: Wired | None = None, **ports: Any) -> OpsServices:
    targets = None
    if wired is not None:
        auditor = AccessAuditor(env.engine, env.clock, wired.events)
        targets = KavitaTargetOps(
            env.engine,
            clock=env.clock,
            client_for=lambda _target_id: wired.rebuild_client(),
            instance=InstanceService(env.engine, env.clock, wired.events),
            auditor=auditor,
        )
    return OpsServices(
        env.engine, clock=env.clock, bus=EventBus(env.clock), fsops=env.lib.fsops,
        targets=targets, **ports,
    )  # fmt: skip


def put_health(env: ExportEnv, **flags: Any) -> TargetHealth:
    """Store a target health with the given flags, as the client would have left it."""
    health = TargetHealth(**flags)
    health.refresh_waiting()
    with env.engine.begin() as connection:
        save_health(connection, env.target_id, health)
    return health


def add_job(engine: sa.Engine, *, kind: str, status: str, code: str | None = None) -> str:
    job_id = new_ulid()
    with engine.begin() as connection:
        connection.execute(
            sa.insert(platform.job_view).values(
                id=job_id, kind=kind, status=status, dedupe_key=f"{kind}:{job_id}",
                error_code=code, last_error="boom" if code else None, attempts=5,
                huey_task_id=new_ulid(),
            )
        )  # fmt: skip
    return job_id


def job_status(engine: sa.Engine, job_id: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(platform.job_view.c.status).where(platform.job_view.c.id == job_id)
            ).scalar_one()
        )


def edition_lang(engine: sa.Engine, edition_id: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(catalog.edition.c.lang).where(catalog.edition.c.id == edition_id)
            ).scalar_one()
        )


def pause_follow(engine: sa.Engine, *, failure_class: str, state: str | None) -> None:
    """The follow cycle paused on a source failure (EF-SRC-026)."""
    pause = {"failure_class": failure_class, "event_type": "source.x", "waiting_state": state,
             "since": NOW.isoformat()}  # fmt: skip
    with engine.begin() as connection:
        connection.execute(
            sa.update(acquisition.source_config)
            .where(acquisition.source_config.c.plugin_id == "suwayomi")
            .values(health={"pause": pause})
        )
