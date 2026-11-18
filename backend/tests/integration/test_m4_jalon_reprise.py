"""Milestone M4, adverse review of the whole milestone ("reprise"): resuming after a crash and the
end-to-end idempotence of the background work, across lots.

The per-lot reviews proved each job against its own kill points. These proofs look at what they
could not see: the call pattern of the production job (a fresh call id for every pass), two jobs of
different lots on the same destination, and the in-memory state a restart loses. Each case is red
today and kept as a strict xfail until the fix (contradicteur de jalon M4, docs/decisions/M4.md).
"""

from __future__ import annotations

import contextlib
import gc
from datetime import timedelta
from typing import Any

import pytest

from shelfhold.export.application import fan_out as fan_out_module
from shelfhold.export.application.fan_out import HostContentStore
from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.remove_destination import (
    RemovalMode,
    RemovalRefusedError,
    removal_phrase,
)
from shelfhold.reading.application.kosync import Kosync
from tests.byte_source_env import unit_with_item
from tests.detach_env import ADMIN
from tests.export_env import ExportEnv, export_env
from tests.fan_out_env import add_target, bind, records_of
from tests.integration.test_l47_amendments import _back_by
from tests.integration.test_m4_l50_relay import PAGES, lab, progress_of, put, three
from tests.integration.test_native_folder_pass import run
from tests.integration.test_native_folder_review_fixes import _remover

__all__ = ["lab"]  # the fixture of the relay chain, asked for by name below


class Killed(BaseException):
    """What a `kill -9` looks like from inside the process: nothing after it runs."""


def _native(env: ExportEnv) -> str:
    return bind(
        env, "local", target_id=add_target(env, kind="native_folder", partitioning="none"),
        link=False,
    )  # fmt: skip


def _files(env: ExportEnv) -> list[str]:
    root = env.lib.manga / "local"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _boot(env: ExportEnv) -> None:
    """The start, in its order: open intents settled, then the locks a dead process left."""
    IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open()
    ResourceLocks(env.engine, clock=env.clock).release_all()


# -- J-REP-1: a pass killed before its manifest is never resumed by the job's next pass ----------


@pytest.mark.req("EF-EXP-010", "EF-EXP-001")
def test_a_pass_killed_before_its_manifest_is_finished_by_the_next_pass_of_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The files of a plan are written one by one; the manifest is written once, after the last
    (`FanOut._keep`). A kill in between leaves files whose intents are `done` and no manifest
    line. The kill -9 proofs of L04/L06/L07 restart with the **same** call id; the export job
    (`runtime_jobs._fan_out_pass`) gives every pass a fresh `new_ulid()`. The next pass then meets
    its own file as "a file the host did not write" and the binding fails at every pass after."""
    with export_env() as env:
        series = env.new_series("Dr Stone")
        units = [unit_with_item(env, series, "1"), unit_with_item(env, series, "2")]
        destination = _native(env)
        fan, _source = run(env)

        def die(*_args: Any, **_kwargs: Any) -> None:
            raise Killed  # the files are down, the manifest is not

        monkeypatch.setattr(fan_out_module, "record_applied", die)
        with pytest.raises(Killed):
            fan.run(env.shelf_id, "pass-before-the-kill")
        monkeypatch.undo()
        assert "Dr Stone/Ch.0001.cbz" in _files(env)  # written before the kill
        _boot(env)
        with env.engine.connect() as connection:
            assert open_intent_paths(connection) == frozenset()

        restart, _source = run(env)  # the job, queued again after the start: a new call id
        outcomes = {o.destination_id: o for o in restart.run(env.shelf_id, "next-pass").outcomes}

        assert outcomes[destination].status == "done", outcomes[destination]
        assert {r.export_item_id for r in records_of(env, destination)} == {
            u.item_id for u in units
        }
        assert _files(env) == ["Dr Stone/Ch.0001.cbz", "Dr Stone/Ch.0002.cbz", "Dr Stone/cover.png"]


# -- J-REP-2: removing a destination runs alongside a pass that writes into it -------------------


@pytest.mark.req("EF-EXP-031", "EF-EXP-010")
def test_a_removal_with_its_files_never_runs_alongside_a_pass_that_writes_into_the_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`remove --delete-files` holds `dest:<id>` exclusive: "no export, scan or staging of it
    runs, and none starts" (`RemoveDestination._held`). The Kavita pipeline holds `path:<dest>/..`
    and so the shared row (M3 L30 B2). The executor of plans (L04), which writes the native folder
    and the CWA drop box, holds no lock at all: the removal reads an empty manifest, deletes
    nothing, forgets the destination, and the files the pass wrote stay in the folder for good."""
    with export_env() as env:
        series = env.new_series("Dr Stone")
        unit_with_item(env, series, "1")
        unit_with_item(env, series, "2")
        destination = _native(env)
        fan, _source = run(env)
        remover = _remover(env)
        mode = RemovalMode.DELETE_FILES
        real = HostContentStore.read
        reads: list[str] = []
        refused: list[str] = []

        def read_then_remove(self: HostContentStore, destination_id: str, ref: Any) -> bytes:
            reads.append(ref.ref)
            if len(reads) == 2:  # the first chapter is on the disk: the admin removes the folder
                try:
                    remover.apply(
                        destination, mode, principal=ADMIN, typed=removal_phrase(destination, mode)
                    )
                except RemovalRefusedError as error:
                    refused.append(error.reason)
            return real(self, destination_id, ref)

        monkeypatch.setattr(HostContentStore, "read", read_then_remove)
        with contextlib.suppress(Exception):  # the end state of the disk is what is judged here
            fan.run(env.shelf_id, "c1")

        # either the removal waits for the pass (refused `busy`, the order is given again), or it
        # ran and nothing Shelfhold wrote is left in the folder of a destination that is gone
        assert refused == ["busy"] or not [p for p in _files(env) if p.endswith(".cbz")], (
            refused,
            _files(env),
        )


# -- J-REP-3: the memory of the kosync echo does not survive a restart ---------------------------


def _restart_kosync() -> None:
    """What a restart of the server does to the kosync service: a new one, nothing in memory. The
    page each token was last given (`Kosync._returned`) lives only there (#301 B1)."""
    for found in gc.get_objects():
        if isinstance(found, Kosync):
            found._returned.clear()


@pytest.mark.req("EF-LEC-009", "EF-LEC-014", "EF-EXP-026")
@pytest.mark.xfail(
    strict=True,
    reason="M4 jalon J-REP-3: a restart between a GET and its echo pushes a skipped chapter read",
)
def test_a_restart_between_a_get_and_its_echo_never_pushes_a_skipped_chapter_read(
    lab: Any,
) -> None:
    """The case of #301 B1, with a restart (an update, a crash) between the `GET` that gives the
    e-reader Kavita's page and the `PUT` that sends it back. The decision says the speed rule and
    the session bound "still contain" it: a move of 20 pages in 3 hours is under both, so
    chapter 2, never opened, is told read to Kavita, which cannot be undone."""
    relay, items, digest = three(lab)
    put(lab, digest, 5)  # the e-reader: chapter 1, page 5
    _back_by(lab, timedelta(hours=3))  # the evening before
    relay.read_in_kavita("mum", items[2], 5)  # in Kavita: chapter 3 opened, chapter 2 skipped
    relay.sync_all()
    assert progress_of(lab, digest)["progress"] == str(2 * PAGES + 5)  # what the GET gives
    _restart_kosync()
    put(lab, digest, 2 * PAGES + 5)  # KOReader goes there and, closed, sends it back
    relay.clock.advance(timedelta(minutes=10))
    relay.sync_all()
    assert relay.kavita_page("mum", items[1]) == 0  # Kavita is never told chapter 2 is read
    assert relay.unit_state("mum", items[1]) != "read"  # nobody turned a page of it
