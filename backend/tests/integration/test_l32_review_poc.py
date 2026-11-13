"""PoC de revue PR #224 (M3 L32) : doivent être ROUGES sur la branche."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.export.application.reconcile_full import FullReconciler, InventoryReport
from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.events import ArrivalEventWatcher, HubSession
from shelfhold.export.targets.kavita.polling import ArrivalPoller
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import export_env
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher
from tests.kavita_env import RecordingEvents, run
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT, wire
from tests.l41_env import context, instance

RS = "\x1e"


@pytest.mark.req("EF-EXP-018")
def test_poc_window_end_stops_the_full_pass(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENF-OPS-014 : la fenêtre est close, aucun SIGTERM. Le hachage complet doit s'arrêter."""
    seen: list[bool] = []

    class Scanner:
        def __init__(self, *_: object, **__: object) -> None: ...

        def scan(self, destination_id: str, **kwargs: Any) -> InventoryReport:
            seen.append(kwargs["should_stop"]())
            return InventoryReport()

    monkeypatch.setattr(rj, "FullReconciler", Scanner)
    with instance(settings, tmp_path) as inst:
        inst.configure()
        ctx = context("reconcile.hash")
        ctx = dataclasses.replace(ctx, window_end=ctx.clock.now() - timedelta(hours=1))
        assert ctx.stop_requested is False
        rj.reconcile_handler(inst.comp, in_window=True)(ctx)
        # either the scan is not started, or it is told to stop at once
        assert seen in ([], [True]), f"window over but should_stop() -> {seen}"


@pytest.mark.req("EF-EXP-034")
def test_poc_event_from_another_kavita_polls_this_destination_with_the_wrong_client() -> None:
    """remote_ref n'est unique que par cible : la bibliothèque 7 d'un autre serveur Kavita
    déclenche un poll de NOTRE destination avec le client de l'AUTRE serveur, qui confirme."""
    with export_env() as env:
        w = wire(env)  # destination of target A, library 7
        (rel,) = w.written()
        folder = f"{REMOTE_ROOT}/{rel}".rsplit("/", 1)[0]
        other = FakeKavita(version="0.9.1.4")  # Kavita B, its own library 7, same mount
        other.index_after_scans("Dr Stone", folder, [w.remote(rel)])
        client_b = KavitaClient(
            build_fetcher(other), BASE_URL, AdminKey(API_KEY), events=RecordingEvents(),
            clock=env.clock,
        )  # fmt: skip
        run(client_b.scan_folder(folder))
        hub = HubSession()
        hub.receive(("{}" + RS).encode())
        frame = {
            "type": 1,
            "target": "SeriesAdded",
            "arguments": [{"name": "SeriesAdded", "body": {"libraryId": LIBRARY_ID}}],
        }
        events = hub.receive((json.dumps(frame) + RS).encode())
        watcher = ArrivalEventWatcher(env.clock, ArrivalPoller(env.engine, env.clock, w.events))
        looked = run(watcher.on_events(client_b, events))  # an event read on Kavita B's hub
        state = env.rows(export.export_record)[0].state
        assert looked == [] and state == "written", (
            f"destination of Kavita A polled through Kavita B: looked={looked}, state={state}"
        )


@pytest.mark.req("EF-EXP-018")
def test_poc_swapped_directory_test_passes_with_a_link_following_walk(tmp_path: Path) -> None:
    """Le test `test_a_swapped_directory_is_not_followed_during_the_walk` permute le dossier APRÈS
    l'avoir listé : même un parcours qui suit les liens passe. Ici, permutation avant l'entrée."""
    with export_env() as env:
        files = _exported(env)
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "private.cbz").write_bytes(b"private")
        folder = files[0].parent
        real = env.lib.fsops

        class Following:  # what the test must catch: a walk that follows links
            def __getattr__(self, name: str) -> Any:
                return getattr(real, name)

            def list_dir(self, local: str, **_: Any) -> dict[str, str]:
                return {
                    n: "dir" if os.path.isdir(f"{local}/{n}") else "file" for n in os.listdir(local)
                }

        swapped: list[bool] = []

        class Swapper(Following):
            def list_dir(self, local: str, **kw: Any) -> dict[str, str]:
                found = super().list_dir(local, **kw)
                if not swapped and local.rstrip("/").endswith(folder.name):
                    swapped.append(True)
                    shutil.rmtree(folder)
                    folder.symlink_to(victim, target_is_directory=True)
                return found

        scanner = FullReconciler(env.engine, Swapper(), clock=env.clock, bus=EventBus(env.clock))
        report = scanner.scan(env.destination_id, in_window=True)
        assert swapped
        # same assertion as the PR test: it still holds although this walk FOLLOWS links
        same = not any("private" in p for p in (*report.orphans, *report.duplicates))
        assert not same, "the PR assertion cannot tell a following walk from a safe one"


def _exported(env: Any) -> list[Path]:
    series = env.new_series()
    env.add_chapter(series, "2")
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    return env.files()
