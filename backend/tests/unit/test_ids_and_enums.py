"""Opaque ULID identifiers and the complete WaitingState enumeration (M1a schema)."""

import re
from datetime import UTC, datetime

import pytest

from shelfhold.events.waiting_state import WaitingState
from shelfhold.infrastructure.ids import is_ulid, new_ulid

CROCKFORD = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

# EF-INT-023 and the schema passes (02 section 3.5, 3.6, 3.7, 3.10, 3.13, 3.14).
SPEC_WAITING_STATES = {
    "missing",
    "paused_foreign_writer",
    "dead_letter",
    "instance_changed",
    "target_forbidden",
    "key_expiring",
    "not_indexed",
    "auth_expired",
    "adoption_refused",
    "rating_pending",
    "withdrawal_pending",
    "follow_suspended",
    "safeguard_backlog",
    "permission_denied",
    "child_exposure",
    "auth_mode_unsupported",
    "coverage_unknown",
    "language_unknown",
    "restored_behind",
    "library_type_changed",
    "foreign_root",
    "root_lost",
    "root_behind",
    "store_lost",
    "source_instance_changed",
    "tombstone_key_unknown",
    "tombstone_lines_owed",
    "route_missing",
    # L60 (EF-EXP-029, EF-EXP-032): the matrix names the sentinel state and the plan blocks on a
    # name collision; both need a way out in `status`
    "mount_sentinel_missing",
    "series_name_collision",
}


@pytest.mark.req("ENF-OPS-020")
def test_api_ids_are_opaque_ulids_generator() -> None:
    first, second = new_ulid(), new_ulid()
    assert CROCKFORD.match(first)
    assert first != second
    assert is_ulid(first)
    assert not is_ulid("1")
    assert not is_ulid(first.lower())
    assert not is_ulid(first[:-1] + "U")  # U is not in Crockford's alphabet


@pytest.mark.req("ENF-OPS-020")
def test_ulid_is_sortable_by_creation_time_even_within_a_millisecond() -> None:
    ids = [new_ulid() for _ in range(2000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


@pytest.mark.req("ENF-OPS-020")
def test_ulid_reveals_only_a_millisecond_timestamp() -> None:
    before = int(datetime.now(UTC).timestamp() * 1000)
    value = new_ulid()
    after = int(datetime.now(UTC).timestamp() * 1000)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    stamp = 0
    for char in value[:10]:
        stamp = stamp * 32 + alphabet.index(char)
    assert before <= stamp <= after


@pytest.mark.req("EF-INT-023")
def test_waiting_state_enumeration_is_complete() -> None:
    assert {state.value for state in WaitingState} == SPEC_WAITING_STATES


@pytest.mark.req("EF-INT-023")
def test_waiting_state_values_are_stable_snake_case_strings() -> None:
    for state in WaitingState:
        assert state.name.lower() == state.value
        assert re.fullmatch(r"[a-z]+(_[a-z]+)*", state.value)
    assert WaitingState("restored_behind") is WaitingState.RESTORED_BEHIND


@pytest.mark.req("ENF-OPS-020")
def test_ulid_stays_monotonic_when_the_clock_steps_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from shelfhold.infrastructure import ids

    now = 1_800_000_000_000_000_000  # ns
    monkeypatch.setattr(ids.time, "time_ns", lambda: now)
    first = ids.new_ulid()
    monkeypatch.setattr(ids.time, "time_ns", lambda: now - 5_000_000_000)  # five seconds back
    second = ids.new_ulid()
    third = ids.new_ulid()
    assert first < second < third
    assert ids.is_ulid(second)


@pytest.mark.req("ENF-OPS-020")
def test_ulid_state_is_reset_in_a_forked_child(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from shelfhold.infrastructure import ids

    monkeypatch.setattr(ids.time, "time_ns", lambda: 1_800_000_000_000_000_000)  # frozen clock
    ids.new_ulid()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: never return into pytest
        try:
            os.write(write_fd, ids.new_ulid().encode())
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    child = os.read(read_fd, 26).decode()
    os.close(read_fd)
    os.close(write_fd)
    parent = ids.new_ulid()
    assert ids.is_ulid(child)
    assert child != parent  # same millisecond, yet no shared counter
    assert child[10:] != ids._encode(ids._last_random, 16)
