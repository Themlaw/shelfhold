"""The window job "Mettre à l'abri" (EF-BIB-038, EF-BIB-042 (4), EF-SRC-009, EF-EXP-064).

`library.safeguard` runs in the maintenance window, after the nightly catch-up of the kept
copies: it copies to /store the Units that are owed a copy (their source is gone or failed twice
on content), by capped batches under the budget of the volume, and then, only on a shelf that
asked for it, lets the service go of the downloads that are now safe to let go of. The rules and
the deeds are in `library.adapters.safeguard_job`; this module only wires them to the
composition root and to the job queue, and tells the service gate what "paused" means.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection

from shelfhold.acquisition.public import FailureLedger
from shelfhold.composition import Composition, KeyringUnavailableError, NoLibraryError
from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.schema import acquisition
from shelfhold.infrastructure.wake import WakeDeferred
from shelfhold.jobs.public import JobContext, JobPools
from shelfhold.library.public import (
    ROLES_UNVERIFIED,
    SERVICE_PAUSED,
    SOURCE_NOT_LOADED,
    CycleReport,
    KeepCopier,
    Safeguard,
    SafeguardCycle,
    SafeguardInventory,
    UpstreamPurge,
)
from shelfhold.sdk.connected import Ticket
from shelfhold.store_jobs import store_budget

__all__ = ["SAFEGUARD_KIND", "register_safeguard_job", "safeguard_handler", "service_refusal"]

log = structlog.get_logger(__name__)

SAFEGUARD_KIND: Final = "library.safeguard"
SAFEGUARD_CRON: Final = "40 3 * * *"  # after the catch-up (03:20), inside the window


def service_refusal(connection: Connection, plugin_id: str, source_key: str) -> str | None:
    """Why the service must not be asked to erase anything now, None when it may be.

    A service paused for any reason (`source_instance_changed`, an expired secret, a version it
    cannot drive) is not acted on; nor is one whose account roles could not be read (the account
    might be too powerful for a delete to be trusted to it, `roles_unverified`); nor an upstream
    source the probe found not loaded (`source.not_loaded`)."""
    ledger = FailureLedger(plugin_id)
    if ledger.paused(connection) is not None:
        return SERVICE_PAUSED
    settings = connection.execute(
        sa.select(acquisition.source_config.c.settings).where(
            acquisition.source_config.c.plugin_id == plugin_id
        )
    ).scalar_one_or_none()
    if isinstance(settings, dict) and settings.get("roles_unverified"):
        return ROLES_UNVERIFIED
    if source_key in ledger.not_loaded(connection):
        return SOURCE_NOT_LOADED
    return None


def safeguard_handler(comp: Composition) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        try:
            with comp.write_context() as w:
                store_dir = str(comp.settings.store_dir)
                journal = IntentJournal(comp.engine, w.fsops, clock=comp.clock)
                safeguard = Safeguard(
                    comp.engine, w.fsops, w.loaded.locate, store_dir, comp.settings.data_dir
                )
                keeper = KeepCopier(
                    comp.engine, w.fsops, w.loaded.locate, store_dir, comp.settings.data_dir,
                    journal, extract=safeguard.run,
                )  # fmt: skip

                def publish(connection: Connection, event: str, payload: dict[str, object]) -> None:
                    comp.bus.publish(connection, event, payload)

                cycle = SafeguardCycle(
                    comp.engine, w.fsops, keeper, w.tombstones, lambda: store_budget(comp),
                    publish, comp.clock,
                )  # fmt: skip
                report = CycleReport()
                batch = cycle.plan(report)
                context.record(**report.counters())
                if batch:
                    context.run_steps(cycle.steps(batch, report))
                    cycle.close(report)
                    context.record(**report.counters())
                if context.stop_requested or context.preempted:
                    return  # the window is closing: the purge waits for tomorrow night
                _release_upstream(comp, context, w.fsops, journal, w.tombstones)
        except (NoLibraryError, KeyringUnavailableError) as error:
            log.info("safeguard.skipped", reason=type(error).__name__)
            context.record(skipped=type(error).__name__)

    return run


def _release_upstream(
    comp: Composition, context: JobContext, fsops: Any, journal: IntentJournal, tombstones: Any
) -> None:
    if not SafeguardInventory(comp.engine).upstream_candidates():
        return  # no shelf asked for it, or nothing fetched is waiting: the service is not asked
    link = comp.suwayomi()
    if link is None:
        context.record(purge_skipped="no_connected_service")
        return
    purge = UpstreamPurge(
        comp.engine, fsops, journal, tombstones, service_refusal, link.service, link.fetcher,
        comp.clock, ticket=lambda token: Ticket(token=token, target_kind="unit"),
    )  # fmt: skip

    def awake() -> AbstractAsyncContextManager[None]:
        target = getattr(link.service, "wake_target", None)
        return comp.wakes().session(target) if target else nullcontext()

    async def once() -> None:
        try:
            context.record(
                **{f"purge_{k}": v for k, v in (await purge.sweep(awake=awake)).counters().items()}
            )
        except WakeDeferred:
            context.record(purge_skipped="wake_deferred")  # asleep: tomorrow night
        finally:
            await link.guarded.aclose()

    asyncio.run(once())


def register_safeguard_job(pools: JobPools, comp: Composition) -> None:
    pools.register(SAFEGUARD_KIND, pool="maint", handler=safeguard_handler(comp), windowed=True)
    pools.scheduler.add(SAFEGUARD_KIND, SAFEGUARD_CRON, jitter_s=120, windowed=True)
