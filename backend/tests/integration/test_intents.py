"""EF-EXP-041 / ENF-OPS-019 / ENF-QUAL-006 (I9): the intent journal, resolved at boot."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import (
    INTENT_SCHEMA_VERSION,
    IntentJournal,
    open_intent_paths,
    skip_covered,
)
from shelfhold.infrastructure.schema import coherence
from tests.intents_env import IntentEnv, make_intent_env, set_restore_epoch, sha


@pytest.fixture
def ienv(tmp_path: Path) -> IntentEnv:
    return make_intent_env(tmp_path)


def states(ienv: IntentEnv) -> dict[str, str]:
    with ienv.engine.connect() as conn:
        rows = conn.execute(sa.select(coherence.fs_intent.c.id, coherence.fs_intent.c.state))
        return {r.id: r.state for r in rows}


def insert_raw_intent(
    ienv: IntentEnv, *, src: Path, dst: Path, data: bytes, **overrides: object
) -> str:
    """An intent as a former build, a snapshot or a crash would have left it."""
    values: dict[str, object] = {
        "id": "01JBZ3X5QH8M2N4P6R7S9T0V1W",
        "op": "rename",
        "intent_schema_version": INTENT_SCHEMA_VERSION,
        "src_path": str(src),
        "dst_path": str(dst),
        "expected_sha256": sha(data),
        "state": "applying",
        "epoch": 0,
    }
    values.update(overrides)
    with ienv.engine.begin() as conn:
        conn.execute(sa.insert(coherence.fs_intent).values(**values))
    return str(values["id"])


def make_file(ienv: IntentEnv, name: str, data: bytes = b"chapter") -> Path:
    path = ienv.fs.manga / "Series" / name
    ienv.fs.fsops.write_atomic(str(path), data, parents=True)
    return path


@pytest.mark.req("EF-EXP-041")
def test_intent_is_committed_before_the_disk_is_touched(ienv: IntentEnv) -> None:
    src = make_file(ienv, "1.cbz")
    dst = src.with_name("1 renamed.cbz")
    journal = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock)
    intent_id = journal.record(
        op="rename", src=str(src), dst=str(dst), expected_sha256=sha(b"chapter")
    )
    assert states(ienv) == {intent_id: "pending"}  # visible from another connection: committed
    assert src.exists()
    with ienv.engine.connect() as conn:
        row = conn.execute(sa.select(coherence.fs_intent)).one()
    assert row.intent_schema_version == INTENT_SCHEMA_VERSION
    assert row.epoch == 0  # instance.restore_epoch at the time of writing


@pytest.mark.req("EF-EXP-041", "ENF-QUAL-006")
def test_open_intents_resolved_before_any_job(ienv: IntentEnv) -> None:
    from shelfhold.jobs.huey_app import JobPools, boot_jobs

    data_dir = ienv.fs.tmp / "data"
    data_dir.mkdir()
    pools = JobPools(data_dir, engine=ienv.engine, clock=ienv.clock, seed="instance-1")
    src = make_file(ienv, "1.cbz")
    dst = src.with_name("1 renamed.cbz")
    intent_id = insert_raw_intent(ienv, src=src, dst=dst, data=b"chapter")
    journal = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock)

    calls: list[str] = []
    real_requeue = pools.ledger.requeue_running

    def requeue() -> list[object]:
        calls.append("jobs")
        return list(real_requeue())

    pools.ledger.requeue_running = requeue  # type: ignore[method-assign]

    class Held:
        def is_held(self) -> bool:
            return True

    def resolve() -> object:
        calls.append("intents")
        assert states(ienv)[intent_id] == "applying"  # still open when the resolver runs
        return journal.resolve_open()

    boot_jobs(pools, instance_lock=Held(), resolve_intents=resolve)
    assert calls == ["intents", "jobs"]
    assert states(ienv)[intent_id] == "done"
    assert dst.read_bytes() == b"chapter"
    assert not src.exists()


@pytest.mark.req("EF-EXP-041")
def test_a_failing_intent_resolution_starts_no_job(ienv: IntentEnv) -> None:
    from shelfhold.jobs.huey_app import JobPools, boot_jobs

    data_dir = ienv.fs.tmp / "data"
    data_dir.mkdir()
    pools = JobPools(data_dir, engine=ienv.engine, clock=ienv.clock, seed="instance-1")

    class Held:
        def is_held(self) -> bool:
            return True

    def broken() -> object:
        raise RuntimeError("disk gone")

    with pytest.raises(RuntimeError):
        boot_jobs(pools, instance_lock=Held(), resolve_intents=broken)
    with ienv.engine.connect() as conn:  # nothing was queued behind the failure
        assert conn.execute(sa.text("SELECT count(*) FROM job_view")).scalar_one() == 0


@pytest.mark.req("EF-EXP-041", "ENF-QUAL-006")
def test_reconcile_skips_paths_under_open_intent(ienv: IntentEnv) -> None:
    """I9: nothing under an open intent is re-exported or re-planned."""
    journal = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock)
    series = ienv.fs.manga / "Series"
    a, b, c = series / "1.cbz", series / "1 renamed.cbz", series / "2.cbz"
    journal.record(op="rename", src=str(a), dst=str(b))
    journal.record(op="rmdir", src=str(ienv.fs.manga / "Old"))  # a folder covers what is below
    done = journal.record(op="rename", src=str(c), dst=str(series / "2 x.cbz"))
    journal.mark_applying(done)
    journal.mark_done(done)

    with ienv.engine.connect() as conn:
        covered = open_intent_paths(conn)
        assert str(a) in covered and str(b) in covered
        assert str(c) not in covered  # a finished intent protects nothing
        candidates = [
            str(a),
            str(b),
            str(c),
            str(ienv.fs.manga / "Old" / "9.cbz"),
            str(series / "3.cbz"),
        ]
        assert skip_covered(conn, candidates) == [str(c), str(series / "3.cbz")]


@pytest.mark.req("EF-EXP-041")
def test_unresolved_drift_keeps_the_intent_open_and_never_reexports(ienv: IntentEnv) -> None:
    """Neither at src nor at dst: reported as drift, left open, nothing recreated."""
    src = ienv.fs.manga / "Series" / "gone.cbz"
    dst = src.with_name("gone renamed.cbz")
    intent_id = insert_raw_intent(ienv, src=src, dst=dst, data=b"x")
    report = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock).resolve_open()
    assert report.drifted == [intent_id]
    assert states(ienv)[intent_id] == "applying"
    assert not src.exists() and not dst.exists()


@pytest.mark.req("ENF-OPS-019")
def test_unknown_intent_schema_not_replayed(ienv: IntentEnv) -> None:
    src = make_file(ienv, "1.cbz")
    dst = src.with_name("1 renamed.cbz")
    intent_id = insert_raw_intent(
        ienv, src=src, dst=dst, data=b"chapter", intent_schema_version="999", state="pending"
    )
    report = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock).resolve_open()
    assert report.refused == [intent_id]
    assert src.read_bytes() == b"chapter" and not dst.exists()  # the disk was not touched
    assert states(ienv)[intent_id] == "pending"  # still open: a newer build resolves it
    with ienv.engine.connect() as conn:
        assert str(src) in open_intent_paths(conn)  # and still protects its paths


@pytest.mark.req("EF-EXP-041", "ENF-QUAL-006")
def test_stale_intent_detected_by_epoch_counter_not_clock(ienv: IntentEnv) -> None:
    """I28/I39: an intent older than `instance.restore_epoch` came back with a snapshot."""
    set_restore_epoch(ienv.engine, 2)
    old_wall_clock = datetime(2001, 1, 1, tzinfo=UTC)
    future_wall_clock = datetime(2999, 1, 1, tzinfo=UTC)

    stale_src = make_file(ienv, "stale.cbz")
    stale_dst = stale_src.with_name("stale renamed.cbz")
    # The wall clock says "recent" (even in the future), the counter says "from before the restore".
    stale = insert_raw_intent(
        ienv, src=stale_src, dst=stale_dst, data=b"chapter", epoch=1,
        id="01JBZ3X5QH8M2N4P6R7S9T0V1X", created_at=future_wall_clock,
    )  # fmt: skip
    live_src = make_file(ienv, "live.cbz")
    live_dst = live_src.with_name("live renamed.cbz")
    # The wall clock says "ancient", the counter says "written in this epoch": replayed normally.
    live = insert_raw_intent(
        ienv, src=live_src, dst=live_dst, data=b"chapter", epoch=2,
        id="01JBZ3X5QH8M2N4P6R7S9T0V1Y", created_at=old_wall_clock,
    )  # fmt: skip

    ienv.clock.set(datetime(1990, 1, 1, tzinfo=UTC))  # the verdict must not read the clock
    report = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock).resolve_open()

    assert report.stale_from_backup == [stale]
    assert states(ienv)[stale] == "stale_from_backup"
    assert stale_src.exists() and not stale_dst.exists()  # neither completed nor cancelled on disk
    assert states(ienv)[live] == "done"
    assert live_dst.exists() and not live_src.exists()
    with ienv.engine.connect() as conn:
        assert str(stale_src) not in open_intent_paths(conn)  # not open any more: reconcile owns it


@pytest.mark.req("EF-EXP-041")
@pytest.mark.parametrize("case", ["target_other_content", "source_other_content", "both_present"])
def test_replay_only_on_an_exactly_known_disk_state(ienv: IntentEnv, case: str) -> None:
    """Anything but source-only or target-only-with-the-expected-file is drift: untouched, open."""
    src = ienv.fs.manga / "Series" / "1.cbz"
    dst = src.with_name("1 renamed.cbz")
    expected = b"chapter"
    if case == "target_other_content":
        ienv.fs.fsops.write_atomic(str(dst), b"someone else", parents=True)
    elif case == "source_other_content":
        ienv.fs.fsops.write_atomic(str(src), b"someone else", parents=True)
    else:
        ienv.fs.fsops.write_atomic(str(src), expected, parents=True)
        ienv.fs.fsops.write_atomic(str(dst), expected)
    before = {p: p.read_bytes() for p in (src, dst) if p.exists()}
    intent_id = insert_raw_intent(ienv, src=src, dst=dst, data=expected)
    report = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock).resolve_open()
    assert report.drifted == [intent_id]
    assert states(ienv)[intent_id] == "applying"
    assert {p: p.read_bytes() for p in (src, dst) if p.exists()} == before


@pytest.mark.req("EF-EXP-041")
def test_in_place_rewrite_with_the_same_identity_is_not_replayed(ienv: IntentEnv) -> None:
    """Identity (inode, size, mtime) alone is forgeable: the sha256 has to match (02 6.7)."""
    import os

    src = make_file(ienv, "1.cbz", b"ORIGINAL" * 10)
    dst = src.with_name("2.cbz")
    journal = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock)
    observed = ienv.fs.fsops.observe(str(src))
    assert observed is not None
    intent_id = journal.record(
        op="rename", src=str(src), dst=str(dst), expected_file_id=observed[1],
        expected_sha256=sha(b"ORIGINAL" * 10),
    )  # fmt: skip
    journal.mark_applying(intent_id)
    stat = src.stat()
    with src.open("r+b") as handle:  # another writer, in place
        handle.write(b"FOREIGN!" * 10)
    os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    report = journal.resolve_open()
    assert report.drifted == [intent_id] and report.replayed == []
    assert src.read_bytes().startswith(b"FOREIGN!") and not dst.exists()

    # An intent recorded without any hash is never replayed either.
    other = make_file(ienv, "3.cbz", b"x")
    bare = journal.record(op="rename", src=str(other), dst=str(other.with_name("4.cbz")))
    journal.mark_applying(bare)
    assert bare in journal.resolve_open().drifted
    assert other.exists()
