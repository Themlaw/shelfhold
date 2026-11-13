"""M3 L32 — EF-EXP-018 (full reconcile) and EF-EXP-034 (arrival confirmed by Kavita's event).

The full pass only *reports*: drift, orphans, duplicates and symbolic links. Every "never" of the
card is a test that tries to break it: nothing is moved, rewritten, deleted or followed, the
administrator's files are not touched, and a forged event never confirms an arrival.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.export.application import reconcile_full
from shelfhold.export.application.reconcile_full import FullReconciler, InventoryReport
from shelfhold.export.targets.kavita.events import (
    ArrivalEventWatcher,
    HubSession,
    ScanEvent,
    parse_hub_frames,
)
from shelfhold.export.targets.kavita.polling import ArrivalPoller
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, export, platform
from tests.export_env import ExportEnv, export_env
from tests.kavita_env import run
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT, Wired, wire
from tests.l32_env import Probe, swap_during_parent_listing
from tests.l41_env import context, instance

RS = "\x1e"  # the record separator of the SignalR JSON protocol


class Spy:
    """The real `Fsops`, with a count of the files it was asked to read."""

    def __init__(self, real: Fsops) -> None:
        self._real = real
        self.hashed: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def sha256_of(self, local: str, **kwargs: Any) -> str:
        self.hashed.append(local)
        return self._real.sha256_of(local, **kwargs)


class Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, _connection: object, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, payload))


def _scanner(env: ExportEnv, fsops: Any = None, bus: Any = None) -> FullReconciler:
    return FullReconciler(
        env.engine, fsops or env.lib.fsops, clock=env.clock, bus=bus or EventBus(env.clock)
    )


def _exported(env: ExportEnv, count: int = 2) -> list[Path]:
    series = env.new_series()
    for number in range(2, 2 + count):
        env.add_chapter(series, str(number))
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    return env.files()


def _snapshot(root: Path) -> dict[str, tuple[str, str]]:
    """Everything under `root`, a link described and never followed: what a pass must not change."""
    found: dict[str, tuple[str, str]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            full = Path(dirpath, name)
            rel = str(full.relative_to(root))
            if full.is_symlink():
                found[rel] = ("link", os.readlink(full))
            elif full.is_dir():
                found[rel] = ("dir", "")
            else:
                found[rel] = ("file", hashlib.sha256(full.read_bytes()).hexdigest())
    return found


# -- EF-EXP-018: the full pass --------------------------------------------------------------------


@pytest.mark.req("EF-EXP-018")
def test_full_reconcile_drift_orphans_duplicates_symlinks_reported() -> None:
    with export_env() as env:
        files = _exported(env, 2)
        files[0].write_bytes(files[0].read_bytes() + b"+edited")  # drift
        stray = env.root / "Elsewhere" / "stray.cbz"  # orphan
        stray.parent.mkdir()
        stray.write_bytes(b"nobody knows me")
        twin = files[1].parent / "twin.cbz"  # duplicate of a managed file
        shutil.copyfile(files[1], twin)
        link = files[1].parent / "link.cbz"  # symbolic link
        os.symlink(files[1], link)
        before = _snapshot(env.root)

        report = _scanner(env).scan(env.destination_id, in_window=True)

        rel = lambda p: str(p.relative_to(env.root))  # noqa: E731
        assert report.drift == (rel(files[0]),)
        assert report.orphans == (rel(stray),)
        assert report.duplicates == (rel(twin),)
        assert report.symlinks == (rel(link),)
        assert report.blocked is None and not report.interrupted
        assert _snapshot(env.root) == before  # reported, never corrected


@pytest.mark.req("EF-EXP-018")
def test_full_reconcile_clean_tree_reports_nothing() -> None:
    with export_env() as env:
        _exported(env, 2)
        report = _scanner(env).scan(env.destination_id, in_window=True)
        assert (report.drift, report.orphans, report.duplicates, report.symlinks) == ((),) * 4


@pytest.mark.req("EF-EXP-018")
def test_full_reconcile_leaves_the_admin_files_and_the_reserved_area_alone() -> None:
    with export_env() as env:
        _exported(env, 1)
        reserved = [p.name for p in env.lib.library.iterdir() if p.name.startswith(".shelfhold")]
        assert reserved  # the sentinel of the physical root is there, and the walk starts below it
        (env.root / ".shelfhold").mkdir(exist_ok=True)
        (env.root / ".shelfhold" / "bin.cbz").write_bytes(b"in the bin")
        (env.root / ".shelfhold-mount-id").write_bytes(b"a sentinel-named file")
        (env.root / "admin-notes.txt").write_bytes(b"the admin's own file")
        before = _snapshot(env.root)
        report = _scanner(env).scan(env.destination_id, in_window=True)
        everything = {*report.drift, *report.orphans, *report.duplicates, *report.symlinks}
        assert not any(p.startswith(".shelfhold") for p in everything)  # reserved: not reported
        assert _snapshot(env.root) == before  # the admin's file is never moved


@pytest.mark.req("EF-EXP-018")
def test_a_symbolic_link_is_reported_as_a_link_never_followed(tmp_path: Path) -> None:
    with export_env() as env:
        files = _exported(env, 1)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.cbz").write_bytes(files[0].read_bytes())  # same bytes as a managed file
        (env.root / "to-dir").symlink_to(outside, target_is_directory=True)
        (env.root / "loop").symlink_to(env.root, target_is_directory=True)  # a cycle
        (files[0].parent / "to-file.cbz").symlink_to(outside / "secret.cbz")
        spy = Spy(env.lib.fsops)
        report = _scanner(env, spy).scan(env.destination_id, in_window=True)
        assert set(report.symlinks) == {"to-dir", "loop", f"{files[0].parent.name}/to-file.cbz"}
        # what is behind a link is never listed, hashed or counted as a copy
        assert not any("secret.cbz" in p for p in (*report.orphans, *report.duplicates))
        assert not any("outside" in p or "to-file" in p for p in spy.hashed)
        assert (outside / "secret.cbz").exists() and (env.root / "to-dir").is_symlink()


@pytest.mark.req("EF-EXP-018")
def test_the_walk_goes_through_fsops_and_the_declared_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any call to `os.*`, `open` or `os.open` made from the module itself fails the test."""

    def watched(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            caller = sys._getframe(1).f_globals["__name__"]
            assert caller != "shelfhold.export.application.reconcile_full", "a path built by hand"
            return real(*args, **kwargs)

        return call

    with export_env() as env:
        _exported(env, 1)
        (env.root / "orphan.cbz").write_bytes(b"x")
        for name in dir(os):
            attribute = getattr(os, name)
            if callable(attribute) and not name.startswith("_") and not isinstance(attribute, type):
                monkeypatch.setattr(os, name, watched(attribute))
        for name in ("islink", "isdir", "isfile", "exists", "realpath", "lexists"):
            monkeypatch.setattr(os.path, name, watched(getattr(os.path, name)))
        monkeypatch.setattr(builtins, "open", watched(builtins.open))
        monkeypatch.setattr(Path, "iterdir", watched(Path.iterdir))
        report = _scanner(env).scan(env.destination_id, in_window=True)
        assert report.orphans == ("orphan.cbz",)


@pytest.mark.req("EF-EXP-018")
def test_the_full_reconcile_module_imports_no_disk_access_of_its_own() -> None:
    tree = ast.parse(Path(reconcile_full.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"os", "pathlib", "shutil", "glob", "io", "tempfile"}
    calls = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "open" not in calls


@pytest.mark.req("EF-EXP-018")
def test_a_swapped_directory_is_not_followed_during_the_walk(tmp_path: Path) -> None:
    """The folder becomes a link *between the listing of its parent and its own listing*, the
    moment a link-following walk would enter it: nothing behind the link is asked, listed or
    hashed, and the target is left as it was."""
    with export_env() as env:
        files = _exported(env, 1)
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "private.cbz").write_bytes(b"private")
        report, asked = swap_during_parent_listing(env, files[0].parent, victim, Probe)
        assert not any("private" in p for p in (*report.orphans, *report.duplicates, *asked))
        assert (victim / "private.cbz").read_bytes() == b"private"


@pytest.mark.req("EF-EXP-018")
def test_duplicates_need_equal_content_not_equal_size() -> None:
    with export_env() as env:
        files = _exported(env, 2)
        same_size = files[0].parent / "same-size.cbz"
        same_size.write_bytes(b"x" * files[0].stat().st_size)  # same size, other bytes
        first, second = env.root / "A" / "one.cbz", env.root / "B" / "two.cbz"
        for path in (first, second):
            path.parent.mkdir()
            path.write_bytes(b"identical orphans")
        report = _scanner(env).scan(env.destination_id, in_window=True)
        assert report.duplicates == ("A/one.cbz", "B/two.cbz")  # two unknown twins
        assert same_size.name in str(report.orphans) and same_size.name not in str(
            report.duplicates
        )
        assert not any(
            p in report.duplicates for p in (str(f.relative_to(env.root)) for f in files)
        )


@pytest.mark.req("EF-EXP-018")
def test_outside_the_window_nothing_is_hashed() -> None:
    with export_env() as env:
        files = _exported(env, 2)
        shutil.copyfile(files[1], files[1].parent / "twin.cbz")
        files[0].write_bytes(files[0].read_bytes() + b"!")  # the size moved: visible without a hash
        spy = Spy(env.lib.fsops)
        report = _scanner(env, spy).scan(env.destination_id, in_window=False)
        assert spy.hashed == []  # ENF-OPS-014: the hash waits for the window
        assert report.drift == (str(files[0].relative_to(env.root)),)
        assert report.duplicates == ()  # a duplicate needs the hash: left for the window
        assert report.orphans == (f"{files[1].parent.name}/twin.cbz",)


@pytest.mark.req("EF-EXP-018")
def test_a_same_size_same_mtime_rewrite_is_drift_in_the_window() -> None:
    with export_env() as env:
        files = _exported(env, 1)
        stat = files[0].stat()
        files[0].write_bytes(b"\0" * stat.st_size)
        os.utime(files[0], ns=(stat.st_atime_ns, stat.st_mtime_ns))
        rel = str(files[0].relative_to(env.root))
        assert _scanner(env).scan(env.destination_id, in_window=False).drift == ()
        assert _scanner(env).scan(env.destination_id, in_window=True).drift == (rel,)


@pytest.mark.req("EF-EXP-018")
def test_a_path_under_an_open_intent_is_left_out_of_the_inventory() -> None:
    with export_env() as env:
        _exported(env, 1)
        (env.root / "Busy").mkdir()
        (env.root / "Busy" / "mid-write.cbz").write_bytes(b"half")
        (env.root / "idle.cbz").write_bytes(b"x")
        with env.engine.begin() as connection:  # a write that has not finished (I9)
            connection.execute(
                sa.insert(coherence.fs_intent).values(
                    id=new_ulid(),
                    op="write",
                    intent_schema_version="1",
                    state="applying",
                    dst_path=f"{env.root}/Busy",
                    epoch=1,
                )
            )
        report = _scanner(env).scan(env.destination_id, in_window=True)
        assert report.orphans == ("idle.cbz",)  # what a job is writing is not a finding yet


@pytest.mark.req("EF-EXP-018")
def test_a_blocked_root_is_not_inventoried() -> None:
    with export_env() as env:
        _exported(env, 1)
        (env.root / "orphan.cbz").write_bytes(b"x")
        sentinels = list(env.lib.library.glob(".shelfhold-mount-id"))
        assert sentinels  # the sentinel of the physical root
        for sentinel in sentinels:
            sentinel.unlink()  # an unmounted disk, a restored backup: nothing may be concluded
        report = _scanner(env).scan(env.destination_id, in_window=True)
        assert report.blocked == "sentinel"
        assert (report.drift, report.orphans, report.duplicates, report.symlinks) == ((),) * 4


@pytest.mark.req("EF-EXP-018")
def test_the_walk_is_bounded_in_depth() -> None:
    with export_env() as env:
        _exported(env, 1)
        deep = env.root.joinpath(*[f"d{i}" for i in range(40)])
        deep.mkdir(parents=True)
        (deep / "deep.cbz").write_bytes(b"deep")
        report = _scanner(env).scan(env.destination_id, in_window=True)  # no recursion error
        assert all(p.count("/") <= 9 for p in report.orphans)


@pytest.mark.req("EF-EXP-018")
def test_a_stop_request_ends_the_pass_without_a_finding() -> None:
    with export_env() as env:
        _exported(env, 2)
        (env.root / "orphan.cbz").write_bytes(b"x")
        bus = Bus()
        report = _scanner(env, bus=bus).scan(
            env.destination_id, in_window=True, should_stop=lambda: True
        )
        assert report.interrupted is True
        assert (report.drift, report.orphans, report.duplicates, report.symlinks) == ((),) * 4
        assert bus.published == []  # a partial view is never announced


@pytest.mark.req("EF-EXP-018")
def test_findings_are_announced_by_kind_and_count_never_by_name() -> None:
    with export_env() as env:
        files = _exported(env, 2)
        (env.root / "orphan-name.cbz").write_bytes(b"x")
        os.symlink(files[0], files[0].parent / "link-name.cbz")
        bus = Bus()
        scanner = _scanner(env, bus=bus)
        scanner.scan(env.destination_id, in_window=True)
        kinds = {p["kind"] for t, p in bus.published if t == "reconcile.drift"}
        assert {"orphan_file", "symlink"} <= kinds
        assert "name" not in json.dumps(bus.published)  # names of files are never published
        assert all(set(p) <= {"destination_id", "kind", "count"} for _t, p in bus.published)
        count = len(bus.published)
        scanner.scan(env.destination_id, in_window=True)
        assert len(bus.published) == count  # unchanged findings are news once, not every pass


@pytest.mark.req("EF-EXP-018")
def test_the_new_kinds_are_accepted_by_the_real_bus() -> None:
    with export_env() as env:
        files = _exported(env, 1)
        (env.root / "orphan.cbz").write_bytes(b"x")
        os.symlink(files[0], files[0].parent / "l.cbz")
        files[0].write_bytes(files[0].read_bytes() + b"!")
        _scanner(env).scan(env.destination_id, in_window=True)
        with env.engine.connect() as connection:
            kinds = {
                row.payload.get("kind")
                for row in connection.execute(
                    sa.select(platform.event.c.payload).where(
                        platform.event.c.type == "reconcile.drift"
                    )
                )
            }
        assert {"modified_file", "orphan_file", "symlink"} <= kinds


@pytest.mark.req("EF-EXP-018")
def test_the_hash_window_job_runs_the_full_pass_and_the_cycle_job_does_not(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    class Scanner:
        def __init__(self, *_: object, **__: object) -> None: ...

        def scan(self, destination_id: str, **kwargs: Any) -> InventoryReport:
            calls.append({"destination": destination_id, **kwargs})
            return InventoryReport()

    monkeypatch.setattr(rj, "FullReconciler", Scanner)
    with instance(settings, tmp_path) as inst:
        inst.configure()
        rj.reconcile_handler(inst.comp, in_window=False)(context("reconcile.cycle"))
        assert calls == []  # the cycle reads sizes and mtimes only
        ctx = SimpleNamespace(
            stop_requested=False, job_id="j", window_end=None, clock=inst.comp.clock
        )
        rj.reconcile_handler(inst.comp, in_window=True)(ctx)  # type: ignore[arg-type]
        assert len(calls) == 1 and calls[0]["in_window"] is True
        assert callable(calls[0]["should_stop"])  # the interruption is read by the handler itself


# -- EF-EXP-034: arrival by Kavita's event, polling as the fallback -------------------------------


def _poller(w: Wired) -> ArrivalPoller:
    return ArrivalPoller(w.env.engine, w.env.clock, w.events)


def _frame(target: str, library_id: int = LIBRARY_ID, **body: object) -> bytes:
    message = {
        "type": 1,
        "target": target,
        "arguments": [{"name": target, "body": {"libraryId": library_id, **body}}],
    }
    return (json.dumps(message) + RS).encode()


def _hub() -> HubSession:
    """A hub session past its handshake: what the transport hands over once Kavita said `{}`."""
    session = HubSession()
    assert (
        session.handshake_request()
        == (json.dumps({"protocol": "json", "version": 1}) + RS).encode()
    )
    session.receive(("{}" + RS).encode())
    return session


def _watcher(w: Wired, **options: Any) -> ArrivalEventWatcher:
    return ArrivalEventWatcher(w.env.clock, _poller(w), **options)


def _state(env: ExportEnv) -> str:
    return env.rows(export.export_record)[0].state


def _ready(w: Wired) -> tuple[str, str]:
    (rel,) = w.written()
    folder = f"{REMOTE_ROOT}/{rel}".rsplit("/", 1)[0]
    w.fake.index_after_scans("Dr Stone", folder, [w.remote(rel)])
    run(w.client.scan_folder(folder))  # Kavita scanned: the event tells so, the API holds it
    return rel, folder


@pytest.mark.req("EF-EXP-034")
def test_arrival_confirmed_by_kavita_signalr_event_with_polling_fallback() -> None:
    with export_env() as env:
        w = wire(env)
        _ready(w)
        # (1) the event: the arrival is confirmed at once, from what the API of Kavita holds
        events = _hub().receive(_frame("SeriesAdded", seriesId=1))
        polled = run(_watcher(w).on_events(w.client, events))
        assert polled == [env.destination_id] and _state(env) == "resolved"
        assert env.rows(export.export_record)[0].remote_ref == str(w.fake.series[0]["id"])
    with export_env() as env:
        w = wire(env)
        _ready(w)
        # (2) no event (a silent hub, no transport): the polling still confirms
        assert run(_poller(w).poll(w.client, env.destination_id)).indexed != []
        assert _state(env) == "resolved"


@pytest.mark.req("EF-EXP-034")
def test_a_forged_event_never_confirms_an_arrival() -> None:
    with export_env() as env:
        w = wire(env)
        w.written()  # written, never scanned, never indexed by Kavita
        events = _hub().receive(_frame("SeriesAdded", seriesId=999))
        run(_watcher(w).on_events(w.client, events))
        assert _state(env) == "written"  # the event is a reason to look, never the proof
        assert env.rows(export.export_record)[0].remote_ref is None
        assert w.events.codes() == []


@pytest.mark.req("EF-EXP-034")
def test_an_event_names_no_state_to_write() -> None:
    """Whatever the body claims (a state, a path, a series), only the API of Kavita is believed."""
    with export_env() as env:
        w = wire(env)
        w.written()
        body = {"state": "resolved", "remoteRef": "1", "folderPath": REMOTE_ROOT, "resolved": True}
        events = _hub().receive(_frame("ScanSeries", **body))
        run(_watcher(w).on_events(w.client, events))
        assert _state(env) == "written"


@pytest.mark.req("EF-EXP-034")
def test_an_event_for_another_library_triggers_nothing() -> None:
    with export_env() as env:
        w = wire(env)
        _ready(w)
        events = _hub().receive(_frame("SeriesAdded", library_id=LIBRARY_ID + 5))
        w.fake.requests.clear()
        assert run(_watcher(w).on_events(w.client, events)) == []
        assert w.fake.requests == [] and _state(env) == "written"  # not even a request


@pytest.mark.req("EF-EXP-034")
def test_removal_and_unknown_messages_confirm_nothing() -> None:
    with export_env() as env:
        w = wire(env)
        _ready(w)
        session = _hub()
        frames = [_frame("SeriesRemoved"), _frame("NotificationProgress"), _frame("Anything")]
        events = [e for frame in frames for e in session.receive(frame)]
        assert events == []  # only a scan or an addition is a reason to look


@pytest.mark.req("EF-EXP-034")
def test_nothing_is_read_before_the_handshake_is_acknowledged() -> None:
    session = HubSession()  # authenticated connection not established yet
    assert session.receive(_frame("SeriesAdded")) == []
    session.receive(('{"error":"Handshake refused"}' + RS).encode())  # a refused handshake
    assert session.receive(_frame("SeriesAdded")) == []
    assert _hub().receive(_frame("SeriesAdded")) == [ScanEvent("SeriesAdded", LIBRARY_ID)]


@pytest.mark.req("EF-EXP-034")
@pytest.mark.parametrize(
    "data",
    [
        b"not json" + RS.encode(),
        b'{"type": 1, "target": "SeriesAdded"}' + RS.encode(),  # no arguments
        b'{"type": 1, "target": "SeriesAdded", "arguments": [{"body": {"libraryId": "x"}}]}'
        + RS.encode(),  # the library is not a number
        b'{"type": 1, "target": "SeriesAdded", "arguments": [{"body": {"libraryId": true}}]}'
        + RS.encode(),
        b'{"type": 6}' + RS.encode(),  # a ping
        b"\xff\xfe" + RS.encode(),
        b"[" * 100_000 + RS.encode(),
        b"x" * (1 << 20),  # oversized
    ],
)
def test_malformed_or_oversized_frames_are_dropped(data: bytes) -> None:
    assert parse_hub_frames(data) == []


@pytest.mark.req("EF-EXP-034")
def test_a_flood_of_events_makes_one_poll_per_interval() -> None:
    with export_env() as env:
        w = wire(env)
        w.written()
        watcher = _watcher(w)
        events = _hub().receive(_frame("SeriesAdded") * 50)
        assert len(events) == 50
        run(watcher.on_events(w.client, events))
        first = len(w.fake.requests)
        run(watcher.on_events(w.client, events))
        assert len(w.fake.requests) == first  # inside the interval: no new request
        env.clock.advance(minutes=1)
        run(watcher.on_events(w.client, events))
        assert len(w.fake.requests) > first  # the interval passed: one more look


@pytest.mark.req("EF-EXP-034")
def test_the_polling_alone_keeps_the_delay_and_not_indexed_when_events_are_silent() -> None:
    with export_env() as env:
        w = wire(env)
        (rel,) = w.written()
        folder = f"{REMOTE_ROOT}/{rel}".rsplit("/", 1)[0]
        run(w.client.scan_folder(folder))
        w.fake.never_indexed.add(folder)
        watcher = _watcher(w)
        env.clock.advance(minutes=31)
        run(watcher.on_events(w.client, _hub().receive(_frame("SeriesAdded"))))
        assert w.events.codes() == ["target.not_indexed"]  # the event path keeps the same ending
        assert len(env.files()) == 1  # nothing exported again


@pytest.mark.req("EF-EXP-034")
def test_an_event_of_another_target_looks_at_nothing_here() -> None:
    with export_env() as env:
        w = wire(env)
        w.written()
        w.client.bind_target("01H00000000000000000000000")  # the hub of some other server
        events = _hub().receive(_frame("SeriesAdded"))
        w.fake.requests.clear()
        assert run(_watcher(w).on_events(w.client, events)) == []
        assert w.fake.requests == [] and _state(env) == "written"


@pytest.mark.req("EF-EXP-018")
def test_a_window_that_closes_stops_the_hash_of_a_large_file() -> None:
    """The stop is read between two blocks of the hash, not only between two files."""
    with export_env() as env:
        _exported(env, 1)
        for name in ("one.cbz", "two.cbz"):  # unknown, same size: both are read for a copy
            (env.root / name).write_bytes(b"\0" * (3 << 20))  # three blocks: a stop lands inside
        hashing: list[bool] = []

        class Hasher(Probe):
            def sha256_of(self, local: str, **kwargs: Any) -> str:
                hashing.append(True)  # from here on the window is over
                return super().sha256_of(local, **kwargs)

        probe = Hasher(env.lib.fsops)
        blocks: list[int] = []

        def over() -> bool:
            if hashing:
                blocks.append(1)
            return bool(hashing)

        report = _scanner(env, probe).scan(env.destination_id, in_window=True, should_stop=over)
        assert report.interrupted is True
        assert len(blocks) == 1  # stopped at the first block of the first file, not at its end
