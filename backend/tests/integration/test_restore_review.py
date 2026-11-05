"""Regression tests for the review of PR #61 (L35): each one is a proof of concept that broke it."""

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.restore import RestoredBehindError
from shelfhold.export.application.roots import load_roots
from shelfhold.export.domain.path_map import PathMap
from shelfhold.export.domain.physical_roots import MAX_RESTORE_EPOCH, RootState
from shelfhold.infrastructure.fsops import Fsops, RootBlockedError, TombstonePendingError
from shelfhold.infrastructure.instance import WriteSessions
from shelfhold.infrastructure.instance.generation import GenerationCounter
from shelfhold.infrastructure.schema import platform
from shelfhold.library.tombstones import ContentHash, SeriesBindingRef, VariantRef
from tests.integration.test_restore_epoch import Purger, new_roots, restored
from tests.restore_env import RestoreEnv, restore_env

SENTINEL = ".shelfhold-mount-id"

pytestmark = pytest.mark.req("EF-INT-026")


def edit_sentinel(path: Path, **fields: Any) -> None:
    target = path / SENTINEL
    document = json.loads(target.read_text("utf-8"))
    document.update(fields)
    target.write_text(json.dumps(document), "utf-8")


def stored(env: RestoreEnv) -> tuple[int, int]:
    with env.engine.connect() as connection:
        row = connection.execute(
            sa.select(platform.instance.c.restore_epoch, platform.instance.c.restore_generation)
        ).one()
    return row.restore_epoch, row.restore_generation


def another_fsops(env: RestoreEnv) -> Fsops:
    """What the export and Kavita wirings do: their own `Fsops`, from the database."""
    with env.engine.connect() as connection:
        paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
        loaded = load_roots(connection, PathMap.same_path(paths))
    return Fsops.open(loaded.path_map, loaded.expected, frozen=loaded.restore_pending)


# -- blocker 1: the instance always starts ---------------------------------------------------


@pytest.mark.parametrize("garbage", ["x", -1, 2**63 - 1, MAX_RESTORE_EPOCH + 1, True, None])
def test_an_unreadable_or_absurd_epoch_is_a_mismatch_never_a_freeze(garbage: object) -> None:
    with restore_env() as env:
        edit_sentinel(env.library, restore_epoch=garbage)
        status = env.guard().enter_restored_behind()
        assert not status.behind and not env.fsops.frozen
        states = {Path(f.path): f.state for f in status.findings}
        assert states[env.library] is RootState.MISMATCH
        assert stored(env) == (0, 0)  # nothing was counted: no overflow, no freeze


def test_the_epoch_increment_is_bounded_and_the_exit_still_works() -> None:
    with restore_env() as env:
        restored(env)
        edit_sentinel(env.library, restore_epoch=MAX_RESTORE_EPOCH)
        status = env.guard().enter_restored_behind()
        assert status.behind and status.epoch <= MAX_RESTORE_EPOCH
        assert stored(env)[0] <= MAX_RESTORE_EPOCH


# -- blocker 3 and minor: the freeze is persisted ---------------------------------------------


def test_the_freeze_is_persisted_and_survives_a_restart_and_a_forged_epoch() -> None:
    with restore_env() as env:
        src = sorted(env.managed())[0]
        IntentJournal(env.engine, env.fsops, clock=env.ex.clock).record(
            op="rename",
            src=str(src),
            dst=str(src.with_name("r.cbz")),
            expected_sha256="sha256:" + hashlib.sha256(src.read_bytes()).hexdigest(),
        )
        edit_sentinel(env.library, restore_epoch=1000)  # forged, without any restore
        assert env.guard().enter_restored_behind().behind
        assert stored(env)[1] > 0
        env.open_instance()  # a restart: the sentinel no longer decides alone
        assert env.fsops.frozen and env.guard().assess().behind
        assert env.guard().enter_restored_behind().behind
        assert stored(env)[0] == 1001  # counted once


def test_every_writer_opened_after_the_freeze_is_frozen() -> None:
    with restore_env() as env:
        restored(env)
        env.guard().enter_restored_behind()
        other = another_fsops(env)  # export wiring, Kavita adoption, pairing, receive
        assert other.frozen
        for target in (env.ex.lib.store / "x.bin", env.library / "manga" / "y.cbz"):
            with pytest.raises(RootBlockedError) as blocked:
                other.write_atomic(str(target), b"x", parents=True)
            assert blocked.value.state is RootState.RESTORED_BEHIND


def test_the_freeze_ends_only_with_the_adoption() -> None:
    with restore_env() as env:
        restored(env)
        env.guard().enter_restored_behind()
        adopter = env.adopter()
        adopter.adopt(confirm=adopter.preview().fingerprint)
        assert stored(env)[1] == 0 and not env.fsops.frozen
        assert not another_fsops(env).frozen and not env.guard().assess().behind


def test_a_pending_deletion_keeps_the_instance_read_only() -> None:
    from tests.integration.test_restore_epoch import purge_after_backup

    with restore_env() as env:
        env.add_chapters(3, 4)
        backup = env.backup()
        env.export()
        purge_after_backup(env, "4")
        env.restore(backup)
        env.guard().enter_restored_behind()
        adopter = env.adopter(purger=Purger())
        result = adopter.adopt(confirm=adopter.preview().fingerprint)
        assert result.pending_deletions and result.deleted == ()
        assert env.fsops.frozen and stored(env)[1] > 0
        assert another_fsops(env).frozen
        with pytest.raises(RestoredBehindError):
            env.guard().require_writable()


def test_huey_runs_nothing_while_the_scheduler_is_held(tmp_path: Path) -> None:
    from huey.exceptions import RetryTask

    from shelfhold.jobs.huey_app import JobPools
    from tests.fakes_clock import ManualClock
    from tests.intents_env import NOW

    with restore_env() as env:
        (tmp_path / "data").mkdir()
        pools = JobPools(tmp_path / "data", engine=env.engine, clock=ManualClock(NOW), seed="s")
        pools.scheduler.hold()
        with pytest.raises(RetryTask):
            pools._execute("01J0000000000000000000JOB0")


# -- blocker 1/3: a root that was away comes back ahead --------------------------------------


def test_a_root_remounted_ahead_of_the_database_is_detected_at_once() -> None:
    with restore_env() as env:
        restored(env)
        os.rename(env.library / SENTINEL, env.library / (SENTINEL + ".away"))
        status = env.guard().enter_restored_behind()
        assert not status.behind  # the root was away: nothing to compare yet
        os.rename(env.library / (SENTINEL + ".away"), env.library / SENTINEL)
        frozen: list[bool] = []

        def on_behind() -> None:
            frozen.append(env.guard().enter_restored_behind().behind)

        sessions = WriteSessions(
            env.fsops,
            instance_id=str(env.sentinel()["instance_id"]),
            clock=env.ex.clock,
            generations=GenerationCounter(env.engine),
            on_behind=on_behind,
        )
        with pytest.raises(RootBlockedError), sessions.open(str(env.library)):
            pass
        assert frozen == [True] and env.fsops.frozen and stored(env)[1] > 0


# -- minor: generations ------------------------------------------------------------------------


def test_a_session_refused_on_an_owed_root_does_not_move_the_generation() -> None:
    with restore_env() as env:
        lib = str(env.library)

        def generation() -> int:
            with env.engine.connect() as connection:
                return int(
                    connection.execute(
                        sa.select(platform.physical_root.c.generation).where(
                            platform.physical_root.c.path_entry == lib
                        )
                    ).scalar_one()
                )

        before = generation()
        env.fsops.enforce_tombstone_flush(lambda path: path == lib)
        for _ in range(2):
            with pytest.raises(TombstonePendingError), env.sessions.open(lib):
                pass
        assert generation() == before
        env.fsops.enforce_tombstone_flush(None)
        env.open_instance()
        assert env.fsops.root_state(lib) is RootState.OK


def test_a_database_one_ahead_is_restamped_not_advanced() -> None:
    with restore_env() as env:
        lib = str(env.library)
        with env.engine.begin() as connection:  # a stop between the commit and the sentinel
            connection.execute(
                sa.update(platform.physical_root)
                .where(platform.physical_root.c.path_entry == lib)
                .values(generation=platform.physical_root.c.generation + 1)
            )
        env.open_instance()
        expected = env.sentinel()["generation"]
        with env.sessions.open(lib):
            pass
        assert env.sentinel()["generation"] == expected + 1  # one step, not two
        with env.engine.connect() as connection:
            db = connection.execute(
                sa.select(platform.physical_root.c.generation).where(
                    platform.physical_root.c.path_entry == lib
                )
            ).scalar_one()
        assert db == env.sentinel()["generation"]


# -- blocker 2: a forged or swapped path never crashes the exit -------------------------------


def forge(env: RestoreEnv, rel_path: str, file_hash: str) -> None:
    mirror = next((env.library / ".shelfhold" / "manifest").rglob("*.jsonl"))
    lines = [json.loads(line) for line in mirror.read_text("utf-8").splitlines()]
    entry = json.loads(json.dumps([ln for ln in lines if ln["kind"] == "entry"][-1]))
    entry["rel_path"], entry["file_hash"] = rel_path, file_hash
    entry["export_item"]["id"] = "01J9ZZZZZZZZZZZZZZZZZZZZZZ"
    with mirror.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def test_a_forged_mirror_path_through_a_symbolic_link_is_unresolved() -> None:
    with restore_env() as env:
        restored(env)
        env.guard().enter_restored_behind()
        outside = env.ex.lib.tmp / "outside"
        outside.mkdir()
        (outside / "x.cbz").write_bytes(b"secret")
        digest = "sha256:" + hashlib.sha256(b"secret").hexdigest()
        os.symlink(outside, env.ex.root / "Evil")
        for path in ("Evil/x.cbz", "../outside/x.cbz", "/etc/passwd", "a/../../x.cbz"):
            forge(env, path, digest)
        preview = env.adopter().preview()  # never raises
        assert not [r for r in preview.readopt if "x.cbz" in r.rel_path or "passwd" in r.rel_path]
        assert ("Evil/x.cbz", "path_escape") in preview.unresolved


def test_a_series_folder_swapped_for_a_link_does_not_crash_the_exit() -> None:
    with restore_env() as env:
        restored(env)
        env.guard().enter_restored_behind()
        series = env.managed()[0].parent
        moved = env.ex.lib.tmp / "moved"
        shutil.move(series, moved)
        os.symlink(moved, series)
        preview = env.adopter().preview()
        assert preview.readopt == () and preview.deletions == ()


# -- minor: the fingerprint covers what is applied --------------------------------------------


def test_the_fingerprint_covers_the_decisions_the_mirror_carries() -> None:
    with restore_env() as env:
        restored(env)
        env.guard().enter_restored_behind()
        before = env.adopter().preview().fingerprint
        for mirror in (env.library / ".shelfhold" / "manifest").rglob("*.jsonl"):
            out = []
            for line in mirror.read_text("utf-8").splitlines():
                document = json.loads(line)
                if document["kind"] == "entry":
                    for covered in document["coverage"]:
                        covered["follow_suppressed"] = True
                    document["age_classified"] = {"min_age": 18}
                out.append(json.dumps(document))
            mirror.write_text("\n".join(out) + "\n", "utf-8")
        adopter = env.adopter()
        after = adopter.preview().fingerprint
        assert after != before
        from shelfhold.export.application.restore import PreviewStaleError

        with pytest.raises(PreviewStaleError):
            adopter.adopt(confirm=before)


# -- minor: tombstones in the layout of the purge (L34) ---------------------------------------


def real_purge(env: RestoreEnv, number: str, *, hashed: bool, series: bool = False) -> None:
    source_key, remote, content = env.variant_ref(number)
    values: VariantRef | SeriesBindingRef
    if series:
        values = SeriesBindingRef("suwayomi", source_key, "series-1")
    else:
        hashes = (ContentHash("content-hash@1", content),) if hashed else ()
        values = VariantRef("suwayomi", source_key, remote, hashes)
    with env.sessions.open(str(env.library), str(env.ex.lib.store)):
        env.tombstones().record(values, new_roots(env), reason="purge_unit")


@pytest.mark.parametrize(("hashed", "series"), [(True, False), (False, False), (False, True)])
def test_tombstones_in_the_layout_of_the_purge_block_the_readoption(
    hashed: bool, series: bool
) -> None:
    with restore_env() as env:
        env.add_chapters(3, 4)
        backup = env.backup()
        env.export()
        real_purge(env, "4", hashed=hashed, series=series)
        env.restore(backup)
        env.guard().enter_restored_behind()
        preview = env.adopter().preview()
        purged = {d.rel_path for d in preview.deletions}
        assert purged  # proposed for deletion, whatever the scope
        assert purged.isdisjoint(r.rel_path for r in preview.readopt)


# -- minor: an intent applied after the backup -------------------------------------------------


def test_an_intent_applied_after_the_backup_repairs_the_manifest() -> None:
    with restore_env() as env:
        src = sorted(env.managed())[0]
        dst = src.with_name("renamed.cbz")
        journal = IntentJournal(env.engine, env.fsops, clock=env.ex.clock)
        journal.record(
            op="rename",
            src=str(src),
            dst=str(dst),
            expected_sha256="sha256:" + hashlib.sha256(src.read_bytes()).hexdigest(),
        )
        backup = env.backup()
        with env.sessions.open(str(env.library)):
            journal.resolve_open()
        assert dst.exists() and not src.exists()
        env.restore(backup)
        env.guard().enter_restored_behind()
        adopter = env.adopter()
        preview = adopter.preview()
        assert preview.report.missing_files == () and preview.report.unknown_files == ()
        adopter.adopt(confirm=preview.fingerprint)
        names = {rel.rsplit("/", 1)[-1] for rel in env.record_states()}
        assert "renamed.cbz" in names and src.name not in names
        assert not env.guard().assess().behind
