"""The daily checks of EF-EXP-049 and EF-EXP-047: the Kavita library types read again, the names
replayed after a major update, and the accounts of every Komga audited. All three are idempotent:
a type already reported, a version already replayed or a proof already recorded is not said twice,
and none writes anything to a server or to a library."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.composition import Composition
from shelfhold.composition_errors import KeyringUnavailableError
from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.library_type import LibraryTypeService
from shelfhold.export.targets.kavita.revalidate import Revalidator
from shelfhold.export.targets.komga.audit import KomgaAuditor
from shelfhold.export.targets.komga.client import KomgaClient
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.secrets.errors import EnvelopeError

__all__ = [
    "connected_komga_clients",
    "komga_accounts_audit",
    "library_type_check",
    "major_update_replay",
]

log = structlog.get_logger(__name__)

Clients = Callable[[], Iterable[tuple[str, KavitaClient]]]
KomgaClients = Callable[[], Iterable[tuple[str, KomgaClient]]]


def library_type_check(
    engine: Engine, clock: Clock, events: TargetEvents, clients: Clients
) -> Callable[[], None]:
    """The body of `target.library_type_check`. A Kavita that does not answer is logged and tried
    again tomorrow: it never pauses a shelf and never stops the other targets."""
    service = LibraryTypeService(engine, clock, events)

    def run() -> None:
        for target_id, client in clients():
            try:
                report = asyncio.run(service.check(client, target_id))
            except TargetError as error:
                log.warning(
                    "target.library_type_check_failed",
                    target_id=target_id, error=type(error).__name__,
                )  # fmt: skip
                continue
            log.info("target.library_type_check", target_id=target_id, changed=len(report.changed))

    return run


def komga_accounts_audit(
    engine: Engine, clock: Clock, events: TargetEvents, clients: KomgaClients
) -> Callable[[], None]:
    """The body of `target.komga_audit` (EF-EXP-047, ENF-SEC-016): the accounts of every Komga
    read again over the shelves bound now and the folders of the links that wait (the trees of
    the shelves below 18+ the server does not index yet). It is what keeps the proof of
    partitioning fresh (36 h, the value of L08b) — a proof nobody refreshed is no proof, and a
    shelf below 18+ is refused with its reason; the journal says `proved=True` only when the
    audit read at least one folder. The audit itself never writes to Komga; a server that does
    not answer is recorded as failed (nothing proved) and tried again tomorrow."""
    auditor = KomgaAuditor(engine, clock, events)

    def run() -> None:
        for target_id, client in clients():
            try:
                report = asyncio.run(auditor.audit(client, target_id))
            except TargetError as error:
                log.warning(
                    "target.komga_audit_failed",
                    target_id=target_id, error=type(error).__name__,
                )  # fmt: skip
                continue
            log.info(
                "target.komga_audit", target_id=target_id, proved=report.proved,
                findings=len(report.findings), failed=report.failed,
            )  # fmt: skip

    return run


def connected_komga_clients(
    comp: Composition,
) -> Callable[[], Iterable[tuple[str, KomgaClient]]]:
    """The client factory of the audit of the Komga accounts (the day's job and the admin's
    on-demand twin): every Komga target that has an address and a sealed key. A row without a
    secret, or an envelope the vault cannot open, is logged (its class and its target, never
    the key) and skipped: it must not stop the audit of the targets that follow."""

    def clients() -> Iterable[tuple[str, KomgaClient]]:
        for target_id in comp.komga_targets():
            try:
                yield target_id, comp.komga_client(target_id)
            except (
                KeyringUnavailableError, LookupError, EnvelopeError, sa.exc.NoResultFound,
            ) as error:  # fmt: skip
                log.info(
                    "target.komga_client_skipped",
                    target_id=target_id,
                    reason=type(error).__name__,
                )
                continue

    return clients


def major_update_replay(
    engine: Engine, clock: Clock, events: TargetEvents, clients: Clients
) -> Callable[[], None]:
    """The body of `target.major_update_replay`: once per version line of Kavita."""
    service = Revalidator(engine, clock, events)

    def run() -> None:
        for target_id, client in clients():
            try:
                report = asyncio.run(service.check(client, target_id))
            except TargetError as error:
                log.warning(
                    "target.major_update_replay_failed",
                    target_id=target_id, error=type(error).__name__,
                )  # fmt: skip
                continue
            log.info(
                "target.major_update_replay", target_id=target_id,
                replayed=report.replayed, rejected=len(report.rejected),
            )  # fmt: skip

    return run
