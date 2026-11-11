"""EF-BIB-052, EF-EXP-064, EF-SRC-033: `purge --now` killed at every step (L17).

A real `SIGKILL` of a forked process at each point of the run, then the restart: the boot settles
what it can, the same command is run again. Whatever the step, the index of /store never cites a
file that is gone, the tombstone is there, the service is asked to erase a download once at most,
and the second run ends the purge with no readable copy left."""

import inspect
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.purge import ALERT_DELETE_UPSTREAM, PurgeService
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import catalog, coherence
from tests.export_env import ExportEnv, export_env
from tests.purge_env import ADMIN, TITLE, PurgeEnv, spread
from tests.purge_m1b_env import (
    FakeService,
    fetched_ticket,
    index_is_coherent,
    index_units,
    make_m1b_env,
    write_index,
)

pytestmark = pytest.mark.req("EF-BIB-052", "EF-EXP-064", "EF-SRC-033")


def die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def before(owner: Any, name: str) -> None:
    """Kill the process when `owner.name` is entered."""
    real = getattr(owner, name)

    def killed(*args: Any, **kwargs: Any) -> Any:
        die()
        return real(*args, **kwargs)

    setattr(owner, name, killed)


def after(owner: Any, name: str) -> None:
    real = getattr(owner, name)

    if inspect.iscoroutinefunction(real):  # the service's calls: the kill follows the awaited call

        async def killed_async(*args: Any, **kwargs: Any) -> Any:
            result = await real(*args, **kwargs)
            die()
            return result

        setattr(owner, name, killed_async)
        return

    def killed(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        die()
        return result

    setattr(owner, name, killed)


def nth_unlink() -> None:
    real, calls = Fsops.purge_unlink, []

    def unlink(self: Fsops, local: str) -> None:
        calls.append(local)
        if len(calls) == 2:
            die()
        real(self, local)

    Fsops.purge_unlink = unlink  # type: ignore[method-assign]


def index_write() -> None:
    real = Fsops.write_atomic

    def write(self: Fsops, local: str, *args: Any, **kwargs: Any) -> Any:
        if local.endswith("INDEX.jsonl"):
            die()
        return real(self, local, *args, **kwargs)

    Fsops.write_atomic = write  # type: ignore[method-assign]


def release_sent() -> None:
    after(FakeService, "release")


def release_unsent() -> None:
    before(FakeService, "release")


POINTS: dict[str, Callable[[], None]] = {
    "before_the_service": lambda: before(PurgeService, "_release_upstream"),
    "release_not_sent": release_unsent,
    "release_sent": release_sent,
    "before_the_index": lambda: before(PurgeService, "_rewrite_index"),
    "index_being_written": index_write,
    "after_the_index": lambda: after(PurgeService, "_rewrite_index"),
    "between_two_copies": nth_unlink,
    "before_the_rows": lambda: before(PurgeService, "_close"),
}


def killed_run(purge: PurgeEnv, unit: str, point: str) -> None:
    pid = os.fork()
    if pid == 0:
        try:
            POINTS[point]()
            purge.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=purge.clock.now())
        finally:
            os._exit(0)  # the kill did not land: the test says so below
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, point


def restart(export: ExportEnv, service: FakeService) -> PurgeEnv:
    """What the boot does, then a new process: the engine is new, the locks of the dead job are
    freed, the intents are settled."""
    export.engine.dispose()
    purge = make_m1b_env(export, service)
    ResourceLocks(export.engine, clock=purge.clock).release_all()
    IntentJournal(export.engine, export.lib.fsops, clock=purge.clock).resolve_open()
    return purge


def rows(export: ExportEnv, table: sa.Table, *where: sa.ColumnElement[bool]) -> int:
    with export.engine.connect() as connection:
        return int(
            connection.execute(
                sa.select(sa.func.count()).select_from(table).where(*where)
            ).scalar_one()
        )


@pytest.mark.parametrize("point", list(POINTS))
def test_purge_killed_at_each_step_never_leaves_a_dead_index_or_erases_twice(
    point: str, tmp_path: Path
) -> None:
    with export_env() as export:
        service = FakeService(tmp_path)
        series = export.new_series(TITLE)
        unit, _ = spread(export, series)
        other, _ = spread(export, series, "2")
        fetched_ticket(export, service, unit, "ticket-1")
        purge = make_m1b_env(export, service)
        write_index(purge)
        assert index_units(purge) == [other, unit] or index_units(purge) == [unit, other]

        killed_run(purge, unit, point)

        # whatever was done, the index cites no file that is gone, and the tombstone stands
        assert index_is_coherent(purge)
        assert rows(export, coherence.content_tombstone) == 2
        assert service.released() in ([], ["ticket-1"])

        again = restart(export, service)
        assert index_is_coherent(again)
        report = again.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=again.clock.now())
        if point == "release_not_sent":
            # the deed was begun and nothing says whether it landed (in `ui_login` another account
            # may hold the file): never sent again; the claim is let go of and the admin told to
            # delete in the service
            assert report.upstream_kept_by_others == 1 and service.released() == []
            assert ALERT_DELETE_UPSTREAM in report.alerts
        assert report.complete
        # one erasure whatever the step, none when the deed was begun and never reached the service
        assert service.released() == ([] if point == "release_not_sent" else ["ticket-1"])
        assert unit not in index_units(again) and other in index_units(again)
        assert index_is_coherent(again)
        stored = [p.name for p in export.lib.files_under(export.lib.store)]
        assert not [n for n in stored if unit in n]  # no orphan copy of the Unit
        assert rows(export, catalog.unit, catalog.unit.c.id == unit) == 0
        assert rows(export, coherence.content_tombstone) == 2  # not written twice
        assert (
            rows(
                export,
                coherence.fs_intent,
                coherence.fs_intent.c.state.in_(("pending", "applying")),
            )
            == 0
        )


def test_a_deed_open_for_the_same_request_is_never_sent_twice(tmp_path: Path) -> None:
    """The window job of L16 and `purge --now` can meet: a deed another one has begun on the same
    download is not repeated by this one (the service takes a second erasure for a second
    account's): the claim is let go of, the file is left to the admin to delete in the service."""
    with export_env() as export:
        service = FakeService(tmp_path)
        unit, _ = spread(export, export.new_series(TITLE))
        request = fetched_ticket(export, service, unit, "ticket-1")
        purge = make_m1b_env(export, service)
        journal = IntentJournal(export.engine, export.lib.fsops, clock=purge.clock)
        from shelfhold.library.adapters.safeguard_job import PinnedCopy

        begun = journal.record(
            op="purge_upstream", expected_file_id=PinnedCopy(0, 0, 0, request_id=request)
        )
        journal.mark_applying(begun)  # the other deed: sent, or about to be
        report = purge.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=purge.clock.now())
        assert report.complete and service.released() == []
        assert report.upstream_kept_by_others == 1 and ALERT_DELETE_UPSTREAM in report.alerts
