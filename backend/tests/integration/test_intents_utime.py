"""EF-EXP-041, EF-EXP-001 (M4 L04): the boot settles a `utime` intent left open by a crash.

The file is the one the intent names (same hash: nothing was rewritten) and the time is put again;
nothing there, the operation never started; another file, nothing is touched."""

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.schema import coherence
from tests.intents_env import IntentEnv, make_intent_env, sha

pytestmark = pytest.mark.req("EF-EXP-041", "EF-EXP-001")
NS = 1_577_934_245_000_000_000
DATA = b"chapter"


@pytest.fixture
def ienv(tmp_path: Path) -> IntentEnv:
    return make_intent_env(tmp_path)


def open_utime(ienv: IntentEnv, path: Path, *, state: str, digest: str) -> str:
    journal = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock)
    intent = journal.record(
        op="utime", dst=str(path), expected_sha256=digest, extra={"mtime_ns": NS}
    )
    if state == "applying":
        journal.mark_applying(intent)
    return intent


def state_of(ienv: IntentEnv, intent: str) -> str:
    with ienv.engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(coherence.fs_intent.c.state).where(coherence.fs_intent.c.id == intent)
            ).scalar_one()
        )


@pytest.mark.parametrize("state", ["pending", "applying"])
def test_a_utime_cut_before_the_time_was_set_is_replayed(ienv: IntentEnv, state: str) -> None:
    path = ienv.fs.manga / "S" / "1.cbz"
    ienv.fs.fsops.write_atomic(str(path), DATA, parents=True)
    intent = open_utime(ienv, path, state=state, digest=sha(DATA))
    report = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock).resolve_open()
    assert report.replayed == [intent] and state_of(ienv, intent) == "done"
    assert path.stat().st_mtime_ns == NS and path.read_bytes() == DATA


def test_a_utime_whose_time_was_already_set_is_closed_the_same_way(ienv: IntentEnv) -> None:
    path = ienv.fs.manga / "S" / "1.cbz"
    ienv.fs.fsops.write_atomic(str(path), DATA, parents=True)
    ienv.fs.fsops.utime(str(path), mtime_ns=NS)
    intent = open_utime(ienv, path, state="applying", digest=sha(DATA))
    IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock).resolve_open()
    assert state_of(ienv, intent) == "done" and path.stat().st_mtime_ns == NS


def test_a_utime_for_a_file_that_is_gone_is_cancelled(ienv: IntentEnv) -> None:
    intent = open_utime(ienv, ienv.fs.manga / "gone.cbz", state="pending", digest=sha(DATA))
    report = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock).resolve_open()
    assert report.abandoned == [intent] and state_of(ienv, intent) == "cancelled"


def test_a_utime_for_another_file_touches_nothing_and_stays_open(ienv: IntentEnv) -> None:
    path = ienv.fs.manga / "S" / "1.cbz"
    ienv.fs.fsops.write_atomic(str(path), b"somebody else's", parents=True)
    before = path.stat().st_mtime_ns
    intent = open_utime(ienv, path, state="applying", digest=sha(DATA))
    report = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock).resolve_open()
    assert intent not in report.replayed and state_of(ienv, intent) == "applying"
    assert path.stat().st_mtime_ns == before
