"""L56: the lease is a versioned on-disk format (ENF-OPS-013, ADR-0039, passe 13 N5).

`schema_version` `major.minor`; a later minor is rewritten with every field it carries; a later
major is the format of another build: its holder is refused and its file is left byte for byte.
Real `WriteSessions` over a real root; the foreign lease is the file another build would write.
"""

import json
from pathlib import Path

import pytest

from shelfhold.infrastructure.instance import (
    HEARTBEAT_INTERVAL,
    LEASE_VALIDITY,
    LeaseConflictError,
    WriteSessions,
)
from tests.fakes_clock import ManualClock
from tests.fsops_env import Env, make_env
from tests.intents_env import NOW

pytestmark = pytest.mark.req("ENF-OPS-013")

ME = "01JBZ3X5QH8M2N4P6R7S9T0AAA"
OTHER = "01JBZ3X5QH8M2N4P6R7S9T0BBB"


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


def lease_file(env: Env) -> Path:
    return env.library / ".shelfhold" / "lease"


def _leave_a_lease_behind(
    env: Env, clock: ManualClock, boot_id: str, lock: Path
) -> dict[str, object]:
    """A run of this instance that died with its lease: what the file said."""
    dead = WriteSessions(env.fsops, instance_id=ME, clock=clock, boot_id=boot_id, lock_path=lock)
    dead.take(str(env.library))
    document: dict[str, object] = json.loads(lease_file(env).read_bytes())
    return document


def test_lease_schema_version_unknown_fields_preserved(env: Env, clock: ManualClock) -> None:
    lock = env.library.parent / "lock"
    document = _leave_a_lease_behind(env, clock, "dead-run", lock)
    assert document["schema_version"] == "1.0"
    newer = {**document, "schema_version": "1.3", "m1b_counter": 7, "m1b_nested": {"a": [1, 2]}}
    lease_file(env).write_text(json.dumps(newer))
    sessions = WriteSessions(
        env.fsops, instance_id=ME, clock=clock, boot_id="new-run", lock_path=lock
    )
    target = env.manga / "S" / "1.cbz"
    with sessions.open(str(target)) as session:
        taken = json.loads(lease_file(env).read_bytes())
        assert taken["boot_id"] == "new-run" and taken["token"] != document["token"]
        assert (taken["m1b_counter"], taken["m1b_nested"]) == (7, {"a": [1, 2]})
        assert taken["schema_version"] == "1.3"  # the version the fields were written under
        clock.advance(seconds=HEARTBEAT_INTERVAL.total_seconds() + 1)
        session.write_atomic(str(target), b"x", parents=True)  # the heartbeat rewrites the lease
        renewed = json.loads(lease_file(env).read_bytes())
        assert renewed["token"] != taken["token"] and renewed["m1b_counter"] == 7
    assert not lease_file(env).exists()


def test_a_lease_without_a_version_is_the_first_format(env: Env, clock: ManualClock) -> None:
    lock = env.library.parent / "lock"
    document = _leave_a_lease_behind(env, clock, "dead-run", lock)
    del document["schema_version"]  # written before the format was versioned
    lease_file(env).write_text(json.dumps(document))
    sessions = WriteSessions(
        env.fsops, instance_id=ME, clock=clock, boot_id="new-run", lock_path=lock
    )
    with sessions.open(str(env.manga / "S" / "1.cbz")):
        assert json.loads(lease_file(env).read_bytes())["schema_version"] == "1.0"


@pytest.mark.parametrize("holder", [OTHER, ME])
def test_a_lease_of_a_newer_major_is_refused_and_never_rewritten(
    env: Env, clock: ManualClock, holder: str
) -> None:
    lock = env.library.parent / "lock"
    document = _leave_a_lease_behind(env, clock, "dead-run", lock)
    foreign = {**document, "instance_id": holder, "schema_version": "2.0", "shape": ["new"]}
    lease_file(env).write_text(json.dumps(foreign))
    raw = lease_file(env).read_bytes()
    clock.advance(seconds=LEASE_VALIDITY.total_seconds() + 60)  # run out: it would be taken over
    sessions = WriteSessions(
        env.fsops, instance_id=ME, clock=clock, boot_id="new-run", lock_path=lock
    )
    with pytest.raises(LeaseConflictError):
        sessions.open(str(env.manga / "S" / "1.cbz")).__enter__()
    assert lease_file(env).read_bytes() == raw  # not corrupted, not taken, not released
    sessions.release(str(env.library))
    assert lease_file(env).read_bytes() == raw
