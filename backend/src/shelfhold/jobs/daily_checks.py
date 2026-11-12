"""The daily Kavita checks of EF-EXP-049: the library types read again, and the names replayed
after a major update. Both are idempotent: a type already reported, or a version already
replayed, is not said twice, and neither writes anything to Kavita or to a library."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable

import structlog
from sqlalchemy import Engine

from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.library_type import LibraryTypeService
from shelfhold.export.targets.kavita.revalidate import Revalidator
from shelfhold.infrastructure.clock import Clock

__all__ = ["library_type_check", "major_update_replay"]

log = structlog.get_logger(__name__)

Clients = Callable[[], Iterable[tuple[str, KavitaClient]]]


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
