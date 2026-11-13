"""The operating side of « Récupérer de l'espace » and of the lift (M3 L26): what the three doors
(the HTTP route, the socket method of the host CLI) hand to the services, in one place.

Nothing here decides: the reserved-action rule, the typed word, the holds and the perimeters are
in `BinReclaimer`, `TombstoneService.lift` and fsops; this module builds them over the composition.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.reclaim import BinReclaimer
from shelfhold.export.application.recycle_sweep import BackupsSince
from shelfhold.export.domain.path_map import PathMode
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.observability.disk_guard import disk_usage
from shelfhold.infrastructure.observability.wiring import thresholds_of
from shelfhold.library.public import check_add_fits, reserve_for
from shelfhold.library.tombstones import TombstoneRoot
from shelfhold.ops.backup.report import retained_since

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["backups_of", "lift_tombstone", "space_check", "trash_empty"]


def backups_of(comp: Composition) -> BackupsSince | None:
    """What the retained integrated backups still cite (EF-BIB-021): a copy of /store that one of
    them may look for stays in the bin."""
    folder = comp.settings.backup_dir
    if folder is None:
        return None

    def since(moment: datetime) -> list[tuple[str, datetime]]:
        return retained_since(comp.engine, folder, moment)

    return since


def trash_empty(comp: Composition, *, typed: str | None, principal: Principal) -> dict[str, Any]:
    with comp.write_context() as w:
        journal = IntentJournal(comp.engine, w.fsops, clock=comp.clock)
        reclaimer = BinReclaimer(
            w.fsops, journal, w.writable_roots, clock=comp.clock,
            fresh_auth_s=comp.settings.fresh_auth_s, hardened=comp.settings.auth_hardened,
            backups=backups_of(comp),
        )  # fmt: skip
        report = reclaimer.empty_trash(principal=principal, typed=typed)
    return {
        "removed": report.removed, "kept": report.kept,
        "held_irreplaceable": report.held_irreplaceable, "held_other": report.held_other,
        "failed": report.failed,
    }  # fmt: skip


def lift_tombstone(
    comp: Composition, *, seq: int, typed: str, principal: Principal
) -> dict[str, Any]:
    """The shared tombstone service of the process, so that the lift is felt by the cycles that
    run now, not at the next restart."""
    settings = comp.settings
    with comp.write_context() as w:
        w.tombstones.use_reserved_policy(
            fresh_auth_s=settings.fresh_auth_s, hardened=settings.auth_hardened
        )
        roots = [
            TombstoneRoot(
                r.root_id, r.path, w.fsops.root_state(r.path) is RootState.OK, r.predecessors
            )
            for r in w.loaded.roots
            if r.mode is PathMode.RW
        ]
        done = w.tombstones.lift(
            roots, seq=seq, confirmation=typed, principal=principal, now=comp.clock.now()
        )
    return {"seq": done.seq, "written": len(done.written), "pending": len(done.pending)}


def space_check(comp: Composition, *, estimated_bytes: int) -> dict[str, Any]:
    """The « Ajouter » sheet: does the estimated size fit in the tightest volume an addition
    writes to (`/store` and `/data`), less the reserve of the disk guard? Refused, with the
    figures, before any write (`AddSpaceRefusedError`)."""
    settings = comp.settings
    marks = thresholds_of(settings)
    tightest: tuple[int, int, int] | None = None  # (usable, free, reserve) of the tightest volume
    for folder in (settings.store_dir, settings.data_dir):
        try:
            usage = disk_usage(folder)
        except OSError:
            continue
        reserve = reserve_for(usage.total, marks)
        if tightest is None or usage.free - reserve < tightest[0]:
            tightest = (usage.free - reserve, usage.free, reserve)
    free, reserve = (tightest[1], tightest[2]) if tightest else (0, 0)  # unknown: refused
    checked = check_add_fits(estimated_bytes, free_bytes=free, reserve_bytes=reserve)
    return {"fits": True, **checked.as_dict()}
