"""Review PoCs of PR #224 (M3 L32): they were committed RED on the branch."""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.export.application.reconcile_full import InventoryReport
from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.events import ArrivalEventWatcher, HubSession
from shelfhold.export.targets.kavita.polling import ArrivalPoller
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import export_env
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher
from tests.kavita_env import RecordingEvents, run
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT, wire
from tests.l32_env import Probe, swap_during_parent_listing
from tests.l41_env import context, instance

RS = "\x1e"


@pytest.mark.req("EF-EXP-018")
def test_poc_window_end_stops_the_full_pass(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENF-OPS-014: the window is over, no SIGTERM. The full hash pass must stop."""
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
    """`remote_ref` is unique per target only: library 7 of another Kavita server must not
    make a poll of OUR destination with the OTHER server's client, which would confirm."""
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
def test_poc_swapped_directory_test_catches_a_link_following_walk(tmp_path: Path) -> None:
    """The swapped-folder scenario (the swap right after the listing of the parent) must tell a
    walk that follows links from a safe one: with a following `list_dir` it sees the victim."""

    class Following(Probe):
        def _list(self, local: str, **_: Any) -> dict[str, str]:
            return {
                n: "dir" if os.path.isdir(f"{local}/{n}") else "file" for n in os.listdir(local)
            }

    with export_env() as env:
        files = _exported(env)
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "private.cbz").write_bytes(b"private")
        _report, asked = swap_during_parent_listing(env, files[0].parent, victim, Following)
        assert any("private" in p for p in asked), "the scenario cannot tell a following walk"


def _exported(env: Any) -> list[Path]:
    series = env.new_series()
    env.add_chapter(series, "2")
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    return env.files()
