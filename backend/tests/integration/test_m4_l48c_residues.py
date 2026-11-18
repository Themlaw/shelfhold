"""L48c: what the second review of L48 left open. C1: a file that an earlier call wrote counts as
done only while the disk still holds it; C2: an age declared below the rating of the joined
Edition is no age; C4: a disk failure of a binding is kept like a refusal."""

from __future__ import annotations

from typing import Any

import pytest

from shelfhold.export.application import fan_out as fan_out_module
from shelfhold.export.application.target_state import load_health
from tests.byte_source_env import unit_with_item
from tests.export_env import export_env
from tests.fan_out_env import records_of
from tests.integration.test_m4_jalon_reprise import Killed, _boot, _files, _native
from tests.integration.test_native_folder_pass import run

CH1 = "Dr Stone/Ch.0001.cbz"


def _killed_pass(env: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    series = env.new_series("Dr Stone")
    unit_with_item(env, series, "1")
    unit_with_item(env, series, "2")
    destination = _native(env)
    fan, _source = run(env)

    def die(*_a: Any, **_k: Any) -> None:
        raise Killed

    monkeypatch.setattr(fan_out_module, "record_applied", die)
    with pytest.raises(Killed):
        fan.run(env.shelf_id, "before-the-kill")
    monkeypatch.undo()
    assert CH1 in _files(env)
    _boot(env)
    return destination


@pytest.mark.req("EF-EXP-010", "EF-EXP-001")
def test_c1_a_file_swapped_after_the_kill_is_never_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    with export_env() as env:
        destination = _killed_pass(env, monkeypatch)
        (env.lib.manga / "local" / CH1).write_bytes(b"someone else wrote this")
        restart, _source = run(env)
        outcome = {o.destination_id: o for o in restart.run(env.shelf_id, "next").outcomes}
        assert outcome[destination].status == "failed"  # refused loudly, as before
        assert (env.lib.manga / "local" / CH1).read_bytes() == b"someone else wrote this"
        assert records_of(env, destination) == []  # never taken into the manifest


@pytest.mark.req("EF-EXP-010", "EF-EXP-001")
def test_c1_a_file_removed_after_the_kill_is_written_again(monkeypatch: pytest.MonkeyPatch) -> None:
    with export_env() as env:
        destination = _killed_pass(env, monkeypatch)
        (env.lib.manga / "local" / CH1).unlink()
        restart, _source = run(env)
        outcome = {o.destination_id: o for o in restart.run(env.shelf_id, "next").outcomes}
        assert outcome[destination].status == "done"
        assert CH1 in _files(env)
        assert len(records_of(env, destination)) == 2


@pytest.mark.req("EF-EXP-010")
def test_c4_a_disk_failure_of_a_binding_is_kept_in_the_health_of_its_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with export_env() as env:
        series = env.new_series("Dr Stone")
        unit_with_item(env, series, "1")
        destination = _native(env)
        fan, _source = run(env)
        pipeline = fan._pipeline  # noqa: SLF001

        def broken(*_a: Any, **_k: Any) -> Any:
            raise OSError("disk")

        monkeypatch.setattr(pipeline, "execute_destination_plan", broken)
        outcome = {o.destination_id: o for o in fan.run(env.shelf_id, "p").outcomes}[destination]
        assert outcome.code == "export.write_failed"
        with env.engine.connect() as connection:
            target = connection.execute(
                __import__("sqlalchemy").text(
                    "SELECT target_id FROM shelf_destination WHERE id = :d"
                ),
                {"d": destination},
            ).scalar_one()
            failures = load_health(connection, target).binding_failures
        assert failures == {destination: "export.write_failed"}
