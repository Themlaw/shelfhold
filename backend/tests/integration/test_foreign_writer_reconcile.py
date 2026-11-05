"""Foreign writer detection and the reduced reconcile (EF-EXP-043, EF-EXP-018), real files."""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.events.bus import EventBus
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.reconcile import Reconciler
from shelfhold.export.domain.reconcile import missing_blocks
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, platform
from tests.export_env import ExportEnv, Series, add_second_shelf, export_env


@dataclass
class Rig:
    env: ExportEnv
    reconciler: Reconciler

    def run(self, destination_id: str | None = None) -> None:
        self.env.pipeline.run(destination_id or self.env.destination_id, job_id=new_ulid())

    def reconcile(self, destination_id: str | None = None, *, in_window: bool = True):  # type: ignore[no-untyped-def]
        return self.reconciler.reconcile(
            destination_id or self.env.destination_id, in_window=in_window
        )

    def states(self, destination_id: str | None = None) -> dict[str, str]:
        rows = self.env.rows(export.export_record)
        want = destination_id or self.env.destination_id
        return {r.rel_path: r.state for r in rows if r.shelf_destination_id == want}

    def events(self, event_type: str) -> list[sa.Row[tuple[object, ...]]]:
        return [r for r in self.env.rows(platform.event) if r.type == event_type]

    def paused_series(self) -> list[str]:
        return [r.id for r in self.env.rows(export.export_series) if r.paused_reason]


def rig(env: ExportEnv) -> Rig:
    return Rig(env, Reconciler(env.engine, env.lib.fsops, clock=env.clock, bus=EventBus(env.clock)))


def exported(env: ExportEnv, count: int = 2) -> tuple[Rig, Series, list[Path]]:
    series = env.new_series()
    for number in range(2, 2 + count):
        env.add_chapter(series, str(number))
    r = rig(env)
    r.run()
    return r, series, env.files()


@pytest.mark.req("EF-EXP-043")
def test_foreign_delete_does_not_cause_reexport_loop() -> None:
    with export_env() as env:
        r, _series, files = exported(env, 1)
        files[0].unlink()  # a script deletes it a first time: it was ours, written minutes ago
        first = r.reconcile()
        assert len(first.reexport) == 1 and r.paused_series() == []
        r.run()
        assert len(env.files()) == 1  # put back once
        env.files()[0].unlink()  # and deleted again: this is someone else's habit
        second = r.reconcile()
        assert len(r.paused_series()) == 1 and second.reexport == ()
        assert len(r.events("target.foreign_writer_detected")) == 1
        r.run()
        r.run()
        assert env.files() == []  # no loop: nothing is written while the series is paused


@pytest.mark.req("EF-EXP-043")
def test_foreign_file_in_managed_dir_pauses_series() -> None:
    with export_env() as env:
        r, series, files = exported(env, 1)
        foreign = files[0].parent / "Dr Stone - Ch.99 (automation).cbz"
        foreign.write_bytes(b"not ours")
        report = r.reconcile()
        assert len(r.paused_series()) == 1 and len(report.paused) == 1
        assert foreign.read_bytes() == b"not ours"  # never touched
        assert len(r.events("target.foreign_writer_detected")) == 1
        env.add_chapter(series, "7")
        applied = env.pipeline.preview(env.destination_id)
        assert applied.ops == () and applied.blocked[0].code == "target.foreign_writer_detected"
        r.run()
        assert len(env.files()) == 2  # the foreign file and chapter 2, nothing new
        r.reconcile()  # a second pass does not announce it again
        assert len(r.events("target.foreign_writer_detected")) == 1
        r.reconciler.resume_series(r.paused_series()[0])
        assert r.paused_series() == []


@pytest.mark.req("EF-EXP-043")
def test_foreign_in_place_modification_detected_by_size_mtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with export_env() as env:
        r, _series, files = exported(env, 2)

        def no_hash(local: str) -> str:
            raise AssertionError("a changed size is enough: no hash outside the window")

        monkeypatch.setattr(env.lib.fsops, "sha256_of", no_hash)
        files[0].write_bytes(files[0].read_bytes() + b"tag")  # modified in place, size changes
        report = r.reconcile(in_window=False)
        assert len(r.paused_series()) == 1 and len(report.paused) == 1
        monkeypatch.undo()
        r.reconciler.resume_series(r.paused_series()[0])
        stamp = files[1].stat()  # mtime alone: the hash decides, only in the window
        os.utime(files[1], ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 5_000_000_000))
        pending = r.reconcile(in_window=False)
        assert pending.suspect and r.paused_series() == []
        same = r.reconcile(in_window=True)  # same bytes: a touch, not a foreign writer
        assert same.suspect == () and r.paused_series() == []
        with files[1].open("r+b") as handle:  # same size, other bytes, other mtime
            handle.write(b"\x00")
        os.utime(files[1], ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 9_000_000_000))
        r.reconcile(in_window=True)
        assert len(r.paused_series()) == 1


@pytest.mark.req("EF-EXP-018")
def test_reconcile_missing_recently_written_triggers_reexport() -> None:
    with export_env() as env:
        r, _series, files = exported(env, 3)
        files[0].unlink()
        env.clock.advance(hours=23)
        report = r.reconcile()
        assert len(report.reexport) == 1 and report.missing == ()
        r.run()
        assert sorted(p.name for p in env.files()) == sorted(p.name for p in files)


@pytest.mark.req("EF-EXP-018")
def test_reconcile_manually_deleted_asks_admin() -> None:
    with export_env() as env:
        r, _series, files = exported(env, 3)
        files[0].unlink()
        env.clock.advance(days=7)
        report = r.reconcile()
        assert report.reexport == () and len(report.missing) == 1
        assert sorted(r.states().values()) == ["missing", "written", "written"]
        r.run()
        assert len(env.files()) == 2  # not re-exported: the admin is asked
        assert r.paused_series() == []


@pytest.mark.req("EF-EXP-018")
def test_adopted_missing_reported_only() -> None:
    with export_env() as env:
        r, _series, files = exported(env, 3)
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.export_record).values(state="adopted"))
        files[0].unlink()
        report = r.reconcile()
        assert len(report.adopted_missing) == 1 and report.reexport == () and report.missing == ()
        assert set(r.states().values()) == {"adopted"}  # nothing changed in the manifest
        assert len(r.events("reconcile.drift")) == 1
        r.run()
        assert len(env.files()) == 2


@pytest.mark.req("EF-EXP-018")
def test_reconcile_unknown_reported_never_touched() -> None:
    with export_env() as env:
        r, _series, _files = exported(env, 2)
        stray = env.root / "Other series" / "x.cbz"
        stray.parent.mkdir()
        stray.write_bytes(b"stray")
        report = r.reconcile()
        assert report.unknown == ("Other series",)
        assert stray.read_bytes() == b"stray" and r.paused_series() == []
        assert len(r.events("reconcile.drift")) == 1


@pytest.mark.req("EF-EXP-018")
def test_reconcile_skips_paths_under_an_open_intent() -> None:
    with export_env() as env:
        r, _series, files = exported(env, 2)
        journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)
        journal.mark_applying(journal.record(op="write", dst=str(files[0])))
        files[0].unlink()
        report = r.reconcile()
        assert report.reexport == () and report.missing == () and report.paused == ()


@pytest.mark.req("EF-EXP-018")
def test_missing_ratio_above_threshold_blocks_reconcile() -> None:
    with export_env() as env:
        r, _series, files = exported(env, 12)
        for path in files:  # an unplugged disk: everything is missing
            path.unlink()
        report = r.reconcile()
        assert report.blocked == "missing_ratio"
        assert set(r.states().values()) == {"written"}  # nothing was decided
        assert report.reexport == () and r.paused_series() == []
        assert len(r.events("mount.sentinel_missing")) == 1
        r.run()
        assert env.files() == []


@pytest.mark.req("EF-EXP-018")
def test_missing_threshold_scoped_per_shelf_destination_with_floor() -> None:
    assert not missing_blocks(total=5, missing=5)  # the floor: 10 files
    assert not missing_blocks(total=400, missing=12)  # 3 %
    assert missing_blocks(total=100, missing=12)
    assert missing_blocks(total=2000, missing=51)  # beyond 50, whatever the share
    assert not missing_blocks(total=20, missing=2)
    with export_env() as env:
        r, _series, files = exported(env, 4)
        _shelf, second = add_second_shelf(env)
        other = env.new_series("One Piece", shelf_id=_shelf)
        for number in range(1, 13):
            env.add_chapter(other, str(number))
        r.run(second)
        for path in (env.lib.manga / "Kavita2").rglob("*.cbz"):
            path.unlink()  # 12 of 12 are missing in the second destination
        for path in files[:3]:
            path.unlink()  # 3 of 4 in the first: under the floor
        assert r.reconcile(second).blocked == "missing_ratio"
        first = r.reconcile(env.destination_id)
        assert first.blocked is None and len(first.reexport) == 3
