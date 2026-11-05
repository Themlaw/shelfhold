"""Regression tests of the review of PR 58 (L34): what a purge must never let come back, and the
reruns that must finish."""

import contextlib
import inspect

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.follow import FollowCycle
from shelfhold.export.application.purge import PurgeCollateralError, PurgeService
from shelfhold.export.targets.kavita.adopt import Adopter
from shelfhold.infrastructure import volumes
from shelfhold.infrastructure.fsops import Fsops, TombstonePendingError
from shelfhold.infrastructure.fsops.confined import ConfinedRoot
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, coherence, platform
from shelfhold.library.adapters.storage import PassthroughStore
from shelfhold.library.public import record_replica
from tests.adopt_env import adoption
from tests.export_env import export_env
from tests.purge_env import ADMIN, TITLE, make_purge_env, spread

pytestmark = pytest.mark.req("EF-BIB-052", "EF-BIB-053")


class Crash(BaseException):
    pass


def run(p, unit):
    return p.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=p.clock.now())


def open_intents(env):
    with env.engine.connect() as c:
        return c.execute(
            sa.select(coherence.fs_intent.c.op).where(
                coherence.fs_intent.c.state.in_(("pending", "applying"))
            )
        ).all()


STEPS = {
    "after_audit": (PurgeService, "_tombstone"),
    "inside_record_after_commit": (Fsops, "append_tombstone_line"),
    "after_tombstone_before_intent": (PurgeService, "_erase_copies"),
    "after_intent_before_unlink": (Fsops, "purge_unlink"),
    "after_deletions_before_close": (PurgeService, "_close"),
}


@pytest.mark.parametrize("step", list(STEPS))
@pytest.mark.parametrize("restart", [True, False])
def test_crash_at_each_step_then_rerun_finishes(step, restart, monkeypatch):
    with export_env() as env:
        p = make_purge_env(env.lib)
        unit, placed = spread(env, env.new_series(TITLE))
        cls, name = STEPS[step]

        def boom(*a, **k):
            raise Crash

        monkeypatch.setattr(cls, name, boom)
        with pytest.raises(Crash):
            run(p, unit)
        monkeypatch.undo()
        q = make_purge_env(env.lib) if restart else p
        report = run(q, unit)
        assert report.complete, report
        assert q.count(coherence.content_tombstone) == 2
        assert open_intents(env) == []
        assert not placed["binned"].exists()


def test_transient_journal_error_is_paid_at_the_rerun_without_restart(monkeypatch):
    with export_env() as env:
        p = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        real = Fsops.append_tombstone_line
        calls = {"n": 0}

        def flaky(self, root_path, line, *, header):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(28, "ENOSPC")
            return real(self, root_path, line, header=header)

        monkeypatch.setattr(Fsops, "append_tombstone_line", flaky)
        first = run(p, unit)
        assert not first.complete  # the line is owed: nothing is deleted before it is written
        assert run(p, unit).complete
        assert p.count(coherence.tombstone_pending_root) == 0


def test_a_purged_file_is_not_adopted_again_and_the_preview_says_so():
    with export_env() as env:
        p = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        with env.engine.connect() as c:
            rel = c.execute(
                sa.text("SELECT rel_path FROM replica WHERE unit_id=:u AND location='library'"),
                {"u": unit},
            ).scalar_one()
        data = (env.lib.library / rel).read_bytes()
        assert run(p, unit).complete
        a = adoption(env)
        a.add_series("Restored", {"Restored Ch.1.cbz": data})
        from tests.kavita_env import run as run_async

        seen = run_async(a.adopter.preview(a.wired.client, env.destination_id))
        assert seen.tombstoned  # the preview names the file it will leave out
        a.adopt()
        with env.engine.connect() as c:
            assert (
                c.execute(sa.text("SELECT count(*) FROM unit WHERE state='adopted'")).scalar_one()
                == 0
            )


def test_tombstones_are_required_by_the_cycles_that_could_bring_content_back():
    for cls in (FollowCycle, PassthroughStore, Adopter):
        parameter = inspect.signature(cls.__init__).parameters["tombstones"]
        assert parameter.default is inspect.Parameter.empty, cls


def test_the_freshness_window_is_not_a_parameter_of_the_command():
    for name in ("purge_unit", "purge_work"):
        assert "fresh_auth_s" not in inspect.signature(getattr(PurgeService, name)).parameters


def test_adoption_root_file_removed_by_hand_lets_the_rerun_finish():
    with export_env() as env:
        p = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        ro_id = new_ulid()
        adopted = env.lib.tmp / "upstream-ro"
        adopted.mkdir()
        (adopted / "ch.cbz").write_bytes(b"ro")
        env.lib.fsops._read_only.add(str(adopted))  # as a mapped adoption root would be
        with env.engine.begin() as c:
            c.execute(
                sa.insert(platform.physical_root).values(
                    root_id=ro_id, path_entry=str(adopted), mode="adopt_ro"
                )
            )
            record_replica(
                c, unit_id=unit, location="adopted_ro", content_hash=None, independent=False,
                mutable_by_third_party=True, physical_root_id=ro_id, rel_path="ch.cbz",
            )  # fmt: skip
        first = run(p, unit)
        assert not first.complete and first.refused[0].exit  # the way out is given
        assert (adopted / "ch.cbz").exists()
        (adopted / "ch.cbz").unlink()  # the admin removes it
        assert run(p, unit).complete
        assert p.count(catalog.unit, catalog.unit.c.id == unit) == 0


def test_a_file_the_user_added_in_the_series_folder_is_never_touched():
    with export_env() as env:
        p = make_purge_env(env.lib)
        unit, _ = spread(env, env.new_series(TITLE))
        folder = next(d for d in env.lib.library.rglob("*") if d.is_dir() and TITLE in d.name)
        foreign = folder / "notes.txt"
        foreign.write_bytes(b"mine")
        run(p, unit)
        assert foreign.read_bytes() == b"mine"


def test_collateral_through_a_content_carrier_is_refused():
    with export_env() as env:
        p = make_purge_env(env.lib)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        neighbour = env.add_chapter(series, "2")
        with env.engine.begin() as c:
            cid = c.execute(
                sa.text("SELECT id FROM content WHERE unit_id=:u"), {"u": unit}
            ).scalar_one()
            c.execute(
                sa.text(
                    "INSERT INTO unit_coverage "
                    "(carrier_type, carrier_id, unit_id, position, extent, origin) "
                    "VALUES ('content', :i, :n, 2, 'full', 'manual')"
                ),
                {"i": cid, "n": neighbour},
            )
        with pytest.raises(PurgeCollateralError):
            run(p, unit)


def test_purge_unlink_swapped_for_a_link_after_the_look_deletes_nothing_outside(monkeypatch):
    with export_env() as env:
        outside = env.lib.tmp / "outside.cbz"
        outside.write_bytes(b"precious")
        victim = env.lib.manga / "victim.cbz"
        env.lib.fsops.write_atomic(str(victim), b"x")
        real = ConfinedRoot.lstat_kind

        def swap(self, rel):
            kind = real(self, rel)
            victim.unlink()
            victim.symlink_to(outside)
            return kind

        monkeypatch.setattr(ConfinedRoot, "lstat_kind", swap)
        with contextlib.suppress(Exception):  # refusing is fine; following is not
            env.lib.fsops.purge_unlink(str(victim))
        assert outside.read_bytes() == b"precious"


def test_erase_data_path_never_follows_a_folder_swapped_for_a_link(tmp_path):
    data = tmp_path / "data"
    (data / "cache" / "thumbnails").mkdir(parents=True)
    victim = tmp_path / "victim" / "01ABC"
    victim.mkdir(parents=True)
    (victim / "precious").write_bytes(b"p")
    (data / "cache" / "thumbnails" / "01").symlink_to(tmp_path / "victim")
    with pytest.raises(ValueError):
        volumes.erase_data_path(data, "cache/thumbnails/01/01ABC")
    assert (victim / "precious").read_bytes() == b"p"
    (data / "cache" / "thumbnails" / "02").mkdir()
    (data / "cache" / "thumbnails" / "02" / "01ABC").symlink_to(tmp_path / "victim")
    assert volumes.erase_data_path(data, "cache/thumbnails/02/01ABC")  # removed as a link
    assert (victim / "precious").exists()


def test_a_root_that_is_owed_lines_gets_no_sentinel_write_and_no_boot_clean_up():
    with export_env() as env:
        fs = env.lib.fsops
        fs.enforce_tombstone_flush(lambda path: True)
        before = (env.lib.library / ".shelfhold-mount-id").read_bytes()
        with pytest.raises(TombstonePendingError):
            fs.stamp_sentinel(str(env.lib.library), generation=7)
        (env.lib.library / ".shelfhold" / "staging" / "orphan").write_bytes(b"x")
        report = fs.boot()
        assert (env.lib.library / ".shelfhold-mount-id").read_bytes() == before
        assert (env.lib.library / ".shelfhold" / "staging" / "orphan").exists()
        assert str(env.lib.library) in report.tombstone_owed
