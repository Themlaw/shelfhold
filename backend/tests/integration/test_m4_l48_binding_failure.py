"""L48 (J-REP-1): a binding that fails has an outcome on the admin's side, and the next pass that
succeeds clears it. Before, the failure only went to the log and lasted until someone read it."""

import pytest

from shelfhold.export.application.target_state import load_health
from tests.byte_source_env import unit_with_item
from tests.export_env import export_env
from tests.fan_out_env import add_target, bind
from tests.integration.test_native_folder_pass import run

pytestmark = pytest.mark.req("EF-EXP-010")


def test_a_binding_that_fails_is_kept_in_the_health_of_its_target_then_cleared() -> None:
    with export_env() as env:
        series = env.new_series("Dr Stone")
        unit_with_item(env, series, "1")
        target = add_target(env, kind="native_folder", partitioning="none")
        destination = bind(env, "local", target_id=target, link=False)
        folder = env.lib.manga / "local" / "Dr Stone"
        folder.mkdir(parents=True)
        stray = folder / "Ch.0001.cbz"
        stray.write_bytes(b"a file the host did not write")  # the path is taken by someone else
        fan, _source = run(env)
        outcome = {o.destination_id: o for o in fan.run(env.shelf_id, "p1").outcomes}[destination]
        assert outcome.status == "failed"
        with env.engine.connect() as connection:
            failures = load_health(connection, target).binding_failures
        assert failures == {destination: outcome.code}  # the admin finds it, with its code
        stray.unlink()
        outcome = {o.destination_id: o for o in fan.run(env.shelf_id, "p2").outcomes}[destination]
        assert outcome.status == "done"
        with env.engine.connect() as connection:
            assert load_health(connection, target).binding_failures == {}
