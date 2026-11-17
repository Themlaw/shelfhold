"""M4 L35: the switch to the preferred team, killed by SIGKILL after every step, then the boot.

Two chapters are served from one team and fetched from the preferred one. The application is
killed after each durable step of its protocol (for each chapter: the intent recorded, marked
applying, the new file staged, swapped in, the old one binned, the intent done, then the flag that
says which team is exported). Whatever the instant:

- the path serves exactly one file per chapter (never none, never two), and a reader of it never
  gets a half file;
- no served file is lost: the old one is at its path or in the bin, once;
- the boot settles the open intents before anything else (`settle_hooks`), and the same order
  given again ends where an uninterrupted one would: both chapters from the preferred team, the
  flags and the files agreeing, one `export_upgrade` row each, no double.
"""

import os
import signal
from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.team_switch import (
    preview_team_switch,
    switch_to_preferred_team,
)
from shelfhold.catalog.public import Catalog
from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from tests.export_env import ExportEnv, export_env
from tests.team_switch_env import (
    RealUpgrader,
    Switch,
    add_team_chapter,
    preferred_variant,
    team_switch,
)
from tests.upgrade_env import bin_files, digest, service

pytestmark = pytest.mark.req("EF-BIB-005", "EF-EXP-041")

# the durable steps of one chapter, then the flag of the team
STEPS: tuple[tuple[type, str], ...] = (
    (IntentJournal, "record"),
    (IntentJournal, "mark_applying"),
    (Fsops, "same_path_stage"),
    (Fsops, "same_path_swap"),
    (Fsops, "same_path_recycle_old"),
    (IntentJournal, "mark_done"),
    (Catalog, "set_preferred"),
)
CHAPTERS = 2


def _die_after(kill_at: int) -> None:
    """In the child only: SIGKILL right after the `kill_at`-th step of the protocol."""
    ticks = [0]
    for owner, name in STEPS:
        real: Callable[..., Any] = getattr(owner, name)

        def wrapper(*args: Any, __real: Callable[..., Any] = real, **kwargs: Any) -> Any:
            result = __real(*args, **kwargs)
            if ticks[0] == kill_at:
                os.kill(os.getpid(), signal.SIGKILL)
            ticks[0] += 1
            return result

        setattr(owner, name, wrapper)


def _two_chapters(env: ExportEnv) -> tuple[Switch, str, bytes, bytes]:
    s = team_switch(env)
    series = s.exported.series
    second = env.add_chapter(series, "2")
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.text("UPDATE unit_variant SET preferred = 1 WHERE unit_id = :u"), {"u": second}
        )
    add_team_chapter(env, s.edition_id, series.binding_id, second, "2", s.team_b, tag=b"two")
    files = env.files()
    assert len(files) == CHAPTERS
    first_old = s.target.read_bytes()
    (other,) = (p for p in files if p != s.target)
    return s, second, first_old, other.read_bytes()


def _boot(env: ExportEnv) -> None:
    ResourceLocks(env.engine, clock=env.clock).release_all()
    IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open(
        on_done=service(env).settle_hook()
    )


@pytest.mark.parametrize("kill_at", range(len(STEPS) * CHAPTERS))
def test_team_switch_killed_resumes_without_loss_or_double(kill_at: int) -> None:
    with export_env() as env:
        s, second, first_old, second_old = _two_chapters(env)
        with env.engine.connect() as connection:
            token = preview_team_switch(connection, s.edition_id).token
        env.engine.dispose()

        pid = os.fork()
        if pid == 0:  # the child: the application, killed after one step
            try:
                env.engine.dispose(close=False)
                _die_after(kill_at)
                switch_to_preferred_team(
                    env.engine, s.edition_id, upgrader=RealUpgrader(env), token=token
                )
            finally:
                os._exit(0)
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL

        _boot(env)  # the locks of the dead process go; every open intent is settled first
        with env.engine.connect() as connection:
            assert open_intent_paths(connection) == frozenset()
        assert len(env.files()) == CHAPTERS  # one file per chapter, never none, never two
        for path in env.files():
            assert path.read_bytes() in {first_old, second_old} or path.read_bytes().startswith(
                b"PK"
            )
        olds = {first_old, second_old}
        binned = [p.read_bytes() for p in bin_files(env)]
        assert len(binned) == len(set(binned)) and set(binned) <= olds  # binned at most once

        # the same order again (a fresh preview: the first one may be stale after the kill)
        with env.engine.connect() as connection:
            again = preview_team_switch(connection, s.edition_id)
        report = switch_to_preferred_team(
            env.engine, s.edition_id, upgrader=RealUpgrader(env), token=again.token
        )
        assert report.blocked == {} and report.pending == ()
        with env.engine.connect() as connection:
            assert preview_team_switch(connection, s.edition_id).to_switch == ()  # nothing left
        assert len(env.files()) == CHAPTERS
        assert sorted(p.read_bytes() for p in bin_files(env)) == sorted(olds)  # each old, once
        for unit in (s.unit_id, second):
            assert preferred_variant(env, unit) != ""
        assert preferred_variant(env, s.unit_id) == s.variant_b
        with env.engine.connect() as connection:
            rows = (
                connection.execute(sa.text("SELECT file_hash FROM export_record")).scalars().all()
            )
            upgrades = connection.execute(
                sa.text("SELECT count(*) FROM export_upgrade")
            ).scalar_one()
        assert sorted(rows) == sorted(digest(p) for p in env.files())  # the manifest says the disk
        assert upgrades == CHAPTERS  # one per chapter: the killed one was settled, not redone
