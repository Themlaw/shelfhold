"""The one restore freeze (M1b L02, D11): a persisted generation and a process flag, two states
of one type, with a shape that takes the per-root freezes of L25."""

import pytest

from shelfhold.infrastructure.freeze import FreezeState

pytestmark = pytest.mark.req("EF-INT-026")


def test_a_fresh_state_is_not_frozen_and_the_process_flag_freezes_it() -> None:
    state = FreezeState()
    assert not state.frozen and state.generation == 0
    state.freeze()
    assert state.frozen
    state.thaw()
    assert not state.frozen


def test_a_persisted_generation_freezes_every_root_and_a_fork_is_independent() -> None:
    state = FreezeState(generation=2)
    assert state.frozen and state.frozen_for("any-root")
    twin = state.fork()
    twin.thaw()
    assert not twin.frozen and state.frozen  # a local thaw never reaches the original


def test_a_root_freeze_leaves_the_other_roots_writable() -> None:
    state = FreezeState(roots=frozenset({"root-a"}))
    assert state.frozen_for("root-a") and not state.frozen_for("root-b")
    assert not state.frozen  # the instance level is untouched
