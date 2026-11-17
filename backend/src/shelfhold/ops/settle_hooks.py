"""The one table of the hooks that settle an intent the disk got ahead of (EF-EXP-016, EF-EXP-019).

The boot and `intents resolve` (replay) both settle an open intent: a type of intent that owes
bookkeeping once its disk step is done has its hook here, once, so that neither way can forget it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa

from shelfhold.events.bus import EventBus
from shelfhold.export.application.assembly import VolumeAssembly
from shelfhold.export.application.intents import OpenIntent
from shelfhold.export.application.library_repair import LibraryRepair
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.manifest_write import adopt_written_hook
from shelfhold.export.application.rename_plan import RenameBook
from shelfhold.export.application.upgrade import SamePathUpgrade
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.library.public import complete_interrupted_keep

__all__ = ["settle_hooks"]


def settle_hooks(
    engine: sa.Engine,
    fsops: Fsops,
    locate: Callable[[str], tuple[str, str]],
    *,
    data_dir: Path,
    clock: Clock,
    bus: EventBus | None = None,
) -> Callable[[sa.Connection, OpenIntent], None]:
    """The hook to give `resolve_open` / `resolve_one` as `on_done`: every bookkeeping owed by an
    intent whose disk step is done, whatever its type."""
    adopt = adopt_written_hook(fsops, locate, clock)
    upgraded = SamePathUpgrade(
        engine,
        fsops,
        locate=locate,
        data_dir=data_dir,
        clock=clock,
        bus=bus,
    ).settle_hook()
    assembled = VolumeAssembly(
        engine,
        fsops,
        locate=locate,
        data_dir=data_dir,
        clock=clock,
        bus=bus,
    ).settle_hook()
    repaired = LibraryRepair(
        engine, fsops, locate, clock,
        hold_work=lambda _work: contextlib.nullcontext(), bus=bus,
    ).settle_hook()  # fmt: skip
    renamed = RenameBook(
        engine, fsops, locate=locate, clock=clock,
        locks=ResourceLocks(engine, clock=clock),
    ).settle_hook()  # fmt: skip

    def hook(connection: sa.Connection, intent: OpenIntent) -> None:
        adopt(connection, intent)
        renamed(connection, intent)
        upgraded(connection, intent)
        assembled(connection, intent)
        repaired(connection, intent)
        if intent.op == "copy_cross_root" and intent.dst is not None:
            complete_interrupted_keep(  # a kept copy on disk, its bookkeeping owed
                connection, fsops, locate, src=intent.src, dst=intent.dst, now=clock.now(),
            )  # fmt: skip

    return hook
