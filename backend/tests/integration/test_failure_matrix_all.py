"""ENF-OPS-011 (M3): every dependency of the failure matrix, end to end.

An infrastructure failure pauses the queue concerned and never counts as a content failure; every
row has an injection (or the real test that plays it), a waiting state, an event code and an action
the interface shows. The expectations below are written here, apart from the matrix: a row cannot
satisfy them by carrying a default.
"""

from pathlib import Path

import pytest

from shelfhold.events.operating import OPERATING_EVENTS, WAITING_STATE_EVENT
from shelfhold.i18n import load_catalog
from shelfhold.identity.domain.catalogue import EXIT_ROUTES
from shelfhold.infrastructure.observability.failure_matrix import FAILURE_MATRIX, FailureRow
from shelfhold.interfaces.http_api.catalog import all_error_codes
from shelfhold.ops.exits import ENDPOINTS, bare
from shelfhold.ops.status import command_for
from tests.faults.test_failure_matrix import _test_source
from tests.support.faults import OBSERVATIONS, SCENARIOS

# Dependencies of the L16 fiche: each owns at least one row of the matrix.
DEPENDENCIES = {
    "kavita", "suwayomi", "metadata", "lncrawl", "notifications",
    "disk", "mount", "store", "instance",
}  # fmt: skip

# What each injection leaves behind: the waiting state reported by the production code and the
# command the interface offers (None: the dependency returns by itself, the banner has the text).
EXPECTED = {
    "kavita_unreachable": (None, None),
    "suwayomi_unreachable": (None, None),
    "disk_low": (None, None),
    "second_instance": (None, None),
    "series_dir_not_writable": ("permission_denied", "shelfhold target recheck"),
    "mount_missing": ("mount_sentinel_missing", "shelfhold target recheck"),
}

# Events whose waiting state is missing from `WAITING_STATE_EVENT` (the event is shared).
STATE_OF_EVENT = {"mount.sentinel_missing": "mount_sentinel_missing"}


def _row(key: str) -> FailureRow:
    return next(r for r in FAILURE_MATRIX if r.key == key)


@pytest.mark.req("ENF-OPS-011")
def test_infra_failure_pauses_not_failures(tmp_path: Path) -> None:
    """Every injected infrastructure fault pauses or refuses, and counts no source failure."""
    for number, (key, play) in enumerate(sorted(OBSERVATIONS.items())):
        folder = tmp_path / f"run{number}"
        folder.mkdir()
        seen = play(folder)
        assert seen.paused, f"{key}: the fault did not pause or refuse anything"
        assert seen.counted_failures == 0, f"{key}: an infrastructure fault counted a failure"
        assert seen.code == _row(key).event, key


@pytest.mark.req("ENF-OPS-011")
def test_a_content_failure_is_not_mistaken_for_an_outage(tmp_path: Path) -> None:
    """Attack: the pause must not swallow a real failure; a source that answers keeps counting."""
    from shelfhold.sdk.source import TemporarilyUnavailable
    from tests.follow_env import follow_bench

    with follow_bench() as bench:
        bench.run()
        original = bench.service.followed_series

        async def down(fetcher: object) -> object:
            raise TemporarilyUnavailable("connection refused")

        bench.service.followed_series = down  # type: ignore[method-assign]
        for _ in range(3):
            assert bench.run() is not None
        assert bench.rows("SELECT * FROM source_failure") == []
        bench.service.followed_series = original  # the dependency is back: the pause ends alone
        assert not bench.run().unavailable
        assert bench.run().paused is None


@pytest.mark.req("ENF-OPS-011")
def test_failure_matrix_every_row_injected(tmp_path: Path) -> None:
    """Every dependency of the fiche has a row; every row is injected here or proved by a real test
    that exists and names the event of the row; every injection leaves the state expected."""
    assert {r.dependency for r in FAILURE_MATRIX} >= DEPENDENCIES
    assert {r.key for r in FAILURE_MATRIX if r.simulated} == set(SCENARIOS) == set(EXPECTED)
    for row in FAILURE_MATRIX:
        if row.simulated:
            continue
        assert row.proof, f"{row.key}: neither injected nor proved"
        if row.proof.startswith("lot "):
            raise AssertionError(f"{row.key}: still waits for {row.proof!r}")
        source = _test_source(row.proof)
        assert source is not None, f"{row.key}: {row.proof!r} is not a test that exists"
        assert f'"{row.event}"' in source, f"{row.key}: {row.proof} never names {row.event}"
    for number, (key, play) in enumerate(sorted(OBSERVATIONS.items())):
        folder = tmp_path / f"run{number}"
        folder.mkdir()
        assert play(folder).waiting_state == EXPECTED[key][0], key


@pytest.mark.req("ENF-OPS-011")
def test_failure_matrix_rows_have_ui_action(tmp_path: Path) -> None:
    """The action of a row is what the interface offers: the text of its code, and for a state
    that waits for a human, a command that exists in the exit table, the routes and `status`."""
    catalog = load_catalog()
    codes = {c.code: c for c in all_error_codes()}
    for row in FAILURE_MATRIX:
        assert row.event in OPERATING_EVENTS and row.event in codes, row.key
        title, action = (
            catalog.get(codes[row.event].title_key),
            catalog.get(codes[row.event].action_key),
        )
        assert title and action and action != title, f"{row.key}: no distinct action text"
        if not row.needs_human:
            assert row.exit_command is None, row.key
            continue
        if row.event == "notify.failed":
            continue  # never sent through Apprise; the banner of the page carries the action
        assert row.exit_command, f"{row.key}: a human must act, so a command must exist"
        command = bare(row.exit_command)
        assert command in ENDPOINTS, f"{row.key}: {command!r} is served by no route"
        state = STATE_OF_EVENT.get(row.event) or next(
            (str(s) for s, e in WAITING_STATE_EVENT.items() if e == row.event), None
        )
        if state is not None:
            assert any(
                r.waiting_state == state and bare(r.command) == command for r in EXIT_ROUTES
            ), f"{row.key}: {command!r} does not leave {state!r}"
            assert command in command_for(state, "x")[0], f"{row.key}: status shows another command"
    for number, (key, play) in enumerate(sorted(OBSERVATIONS.items())):
        state, command = EXPECTED[key]
        if state is None:
            continue
        folder = tmp_path / f"run{number}"
        folder.mkdir()
        assert play(folder).waiting_state == state
        assert command in command_for(state, "")[0], key
