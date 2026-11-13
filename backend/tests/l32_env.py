"""M3 L32: the swapped-folder scenario of the full reconcile (test and review PoC)."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shelfhold.events.bus import EventBus
from shelfhold.export.application.reconcile_full import FullReconciler, InventoryReport
from tests.export_env import ExportEnv


class Probe:
    """An `Fsops` that records every path the walk asks about, and runs `on_list` after the
    listing of a folder (where an attacker acts between two steps of the walk)."""

    def __init__(self, real: Any, on_list: Callable[[str], None] | None = None) -> None:
        self._real = real
        self._on_list = on_list
        self.asked: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def _note(self, local: str) -> None:
        self.asked.append(local)

    def list_dir(self, local: str, **kwargs: Any) -> Any:
        self._note(local)
        found = self._list(local, **kwargs)
        if self._on_list is not None:
            self._on_list(local)
        return found

    def _list(self, local: str, **kwargs: Any) -> Any:
        return self._real.list_dir(local, **kwargs)

    def observe(self, local: str, **kwargs: Any) -> Any:
        self._note(local)
        return self._real.observe(local, **kwargs)

    def sha256_of(self, local: str, **kwargs: Any) -> str:
        self._note(local)
        return self._real.sha256_of(local, **kwargs)


def swap_during_parent_listing(
    env: ExportEnv, folder: Path, victim: Path, wrap: Callable[[Any, Callable[[str], None]], Any]
) -> tuple[InventoryReport, list[str]]:
    """Run the full pass while `folder` (a managed series folder) is replaced by a link to
    `victim` *right after its parent was listed*, so the walk is about to enter it. `wrap` builds
    the `Fsops` stand-in from the real one and the swap hook. Returns the report and every path
    the walk asked about."""
    swapped: list[bool] = []

    def swap(listed: str) -> None:
        if not swapped and listed.rstrip("/") == str(folder.parent):
            swapped.append(True)
            shutil.rmtree(folder)
            folder.symlink_to(victim, target_is_directory=True)

    probe = wrap(env.lib.fsops, swap)
    scanner = FullReconciler(env.engine, probe, clock=env.clock, bus=EventBus(env.clock))
    report = scanner.scan(env.destination_id, in_window=True)
    assert swapped, "the swap never happened: the scenario proves nothing"
    return report, probe.asked
