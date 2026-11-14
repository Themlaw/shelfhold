"""The M1b side of `purge --now` (L17): a connected service that holds downloads (nothing leaves
the test), `store/INDEX.jsonl` as the window job writes it, jobs and notifications that cite a
Unit, and the way to look at what is left. Built on `purge_env`: real files, real SQLite, real
cipher."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.purge import PurgeService
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, platform
from shelfhold.library.public import StoreIndex
from shelfhold.purge_upstream import ConnectedUpstream
from shelfhold.safeguard_jobs import service_refusal
from shelfhold.sdk.connected import ReleaseResult, Ticket
from tests.export_env import ExportEnv
from tests.locks_env import index_lock as locks_index
from tests.null_tombstones import NONE_PURGED
from tests.purge_env import PurgeEnv, make_purge_env


@dataclass
class FakeService:
    """The connected service. Its state lives in two files, so that a test can kill the process
    that talks to it and still read what the service was asked."""

    folder: Path
    shared: set[str] = field(default_factory=set)  # held by another account too (ui_login)
    wake_target = None
    unfollowed: list[tuple[str, str, str]] = field(default_factory=list)

    def _held(self) -> set[str]:
        path = self.folder / "held.json"
        return set(json.loads(path.read_text())) if path.exists() else set()

    def hold(self, token: str) -> None:
        (self.folder / "held.json").write_text(json.dumps(sorted(self._held() | {token})))

    def released(self) -> list[str]:
        path = self.folder / "released.log"
        return path.read_text().split() if path.exists() else []

    def cancelled(self) -> list[str]:
        path = self.folder / "cancelled.log"
        return path.read_text().split() if path.exists() else []

    async def poll(self, f: Any, t: Ticket) -> str:
        return "ready" if t.token in self._held() else "gone"

    async def cancel(self, f: Any, t: Ticket) -> None:
        with (self.folder / "cancelled.log").open("a") as log:
            log.write(t.token + "\n")

    async def release(self, f: Any, t: Ticket) -> ReleaseResult:
        if t.token not in self._held():
            return ReleaseResult(released=False)
        with (self.folder / "released.log").open("a") as log:
            log.write(t.token + "\n")
        if t.token in self.shared:  # the file stays: another account keeps it
            return ReleaseResult(released=True, freed_bytes=0, still_present=True)
        (self.folder / "held.json").write_text(json.dumps(sorted(self._held() - {t.token})))
        return ReleaseResult(released=True)

    async def unfollow(self, plugin_id: str, source_key: str, remote_id: str) -> None:
        self.unfollowed.append((plugin_id, source_key, remote_id))


def connected(env: PurgeEnv, service: FakeService) -> ConnectedUpstream:
    return ConnectedUpstream(
        env.lib.engine, env.fsops, IntentJournal(env.lib.engine, env.fsops, clock=env.clock),
        service_refusal, service, object(), env.clock, unfollow=service.unfollow,
    )  # fmt: skip


def make_m1b_env(
    export: ExportEnv,
    service: FakeService | None = None,
    *,
    backups: Any = None,
    index_lock: Any = None,
) -> PurgeEnv:
    # the lock is always explicit: the one the test names, else the real one over this database
    """The purge of M1a, with what M1b adds: /store, the service, the retained backups."""
    lib = export.lib
    env = make_purge_env(lib)
    env.purge = PurgeService(
        lib.engine, lib.fsops, env.tombstones, clock=env.clock, data_dir=lib.data, bus=env.bus,
        fresh_auth_s=900, store_dir=lib.store,
        upstream=connected(env, service) if service is not None else None, backups=backups,
        index_lock=index_lock if index_lock is not None else locks_index(lib.engine),
    )  # fmt: skip
    return env


def write_index(env: PurgeEnv) -> bytes:
    """`store/INDEX.jsonl` as the window job writes it (the title is in it, EF-INT-027)."""
    lib = env.lib
    StoreIndex(
        lib.engine, lib.fsops, str(lib.store), NONE_PURGED, hold=locks_index(lib.engine)
    ).rewrite()
    return (lib.store / "INDEX.jsonl").read_bytes()


def index_units(env: PurgeEnv) -> list[str]:
    path = env.lib.store / "INDEX.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["unit"] for line in path.read_text().splitlines() if line]


def index_is_coherent(env: PurgeEnv) -> bool:
    """Every line of the index names a file that is there: a reader never follows a dead line."""
    path = env.lib.store / "INDEX.jsonl"
    if not path.exists():
        return True
    return all(
        (env.lib.store / json.loads(line)["path"]).is_file()
        for line in path.read_text().splitlines()
        if line
    )


def fetched_ticket(
    export: ExportEnv, service: FakeService, unit: str, token: str, state: str = "fetched"
) -> str:
    """A download request of the Unit's Variant, the service holding the chapter."""
    with write_transaction(export.engine) as connection:
        variant, binding = connection.execute(
            sa.text("SELECT id, source_binding_id FROM unit_variant WHERE unit_id = :u"),
            {"u": unit},
        ).one()
        request = new_ulid()
        connection.execute(
            sa.insert(acquisition.download_request).values(
                id=request, target_kind="unit", variant_id=variant, source_binding_id=binding,
                state=state, ticket=token,
            )
        )  # fmt: skip
    service.hold(token)
    return request


def cite(
    env: PurgeEnv, *, request_id: str | None = None, unit: str | None = None, title: str = ""
) -> dict[str, int | str]:
    """Jobs, events and pending notifications that cite a Unit, a request or (in an error text) a
    title; and one of each that cite nothing, which must stay."""
    ids: dict[str, int | str] = {}
    with write_transaction(env.lib.engine) as connection:
        for name, values in {
            "job_request": {"request_id": request_id},
            "job_dedupe": {"dedupe_key": f"unit:{unit}"},
            "job_error": {"last_error": f"cannot read {title} chapter 1"},
            "job_other": {"dedupe_key": "export"},
        }.items():
            job = new_ulid()
            connection.execute(
                sa.insert(platform.job_view).values(
                    id=job, kind="download", status="failed", **values
                )
            )
            ids[name] = job
        for name, payload in {
            "note_unit": {"unit_id": unit},
            "note_title": {"text": f"{title} is ready"},
            "note_other": {"unit_id": new_ulid()},
        }.items():
            event = connection.execute(
                sa.insert(platform.event).values(
                    type="unit.ready",
                    schema_version=1,
                    payload=payload,
                    occurred_at=datetime(2026, 11, 10, tzinfo=UTC),
                    visibility="ops",
                )
            ).inserted_primary_key[0]
            connection.execute(
                sa.insert(platform.notification_pending).values(
                    id=new_ulid(),
                    event_id=event,
                    channel="apprise:1",
                    next_try_at=datetime(2026, 11, 11, tzinfo=UTC),
                )
            )
            ids[name] = event
    return ids
