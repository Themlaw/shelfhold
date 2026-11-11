"""EF-EXP-016 / EF-EXP-041: a same-path upgrade killed by SIGKILL after every step, then the boot.

Whatever the instant of the kill, the destination serves exactly one file at the path (never
none, never two), the manifest says what the disk says, the old version is never lost (it is at
the path or in the bin) and the upgrade, asked again, ends where an uninterrupted one would.
"""

import errno
import os
import signal
from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.upgrade import UpgradeOutcome
from shelfhold.infrastructure.fsops import Fsops, confined
from shelfhold.infrastructure.schema import export
from tests.export_env import export_env
from tests.upgrade_env import (
    better_copy,
    bin_files,
    digest,
    export_one,
    record_of,
    request,
    service,
)

pytestmark = pytest.mark.req("EF-EXP-016", "EF-EXP-041")

# record (pending), applying, staged, swapped, old binned, done with the manifest
STEPS: tuple[tuple[type, str], ...] = (
    (IntentJournal, "record"),
    (IntentJournal, "mark_applying"),
    (Fsops, "same_path_stage"),
    (Fsops, "same_path_swap"),
    (Fsops, "same_path_recycle_old"),
    (IntentJournal, "mark_done"),
)


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


def _no_exchange(*_args: object, **_kwargs: object) -> None:
    raise OSError(errno.EINVAL, "no RENAME_EXCHANGE here")


@pytest.mark.parametrize("exchange", [True, False], ids=["exchange", "copy_then_replace"])
@pytest.mark.parametrize("kill_at", range(len(STEPS)))
def test_upgrade_interrupted_resumes_from_intent(
    kill_at: int, exchange: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not exchange:
        monkeypatch.setattr(confined, "renameat2_exchange", _no_exchange)
    with export_env() as env:
        exported = export_one(env)
        old_bytes = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        env.engine.dispose()

        pid = os.fork()
        if pid == 0:  # the child: the upgrade, killed after one step
            try:
                env.engine.dispose(close=False)
                _die_after(kill_at)
                service(env).upgrade(request(env, exported, content_id))
            finally:
                os._exit(0)  # only when no kill point was reached
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL

        # The restart: every open intent is settled before anything else.
        upgrade = service(env)
        IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open(
            on_done=upgrade.settle_hook()
        )
        with env.engine.connect() as connection:
            assert open_intent_paths(connection) == frozenset()
        assert env.files() == [exported.target]  # one file served, at the same path
        served = exported.target.read_bytes()
        assert record_of(env, exported).file_hash == digest(exported.target)
        if served != old_bytes:
            assert [p.read_bytes() for p in bin_files(env)] == [old_bytes]

        # Asked again, the upgrade ends where an uninterrupted one would.
        again = upgrade.upgrade(request(env, exported, content_id))
        assert again.outcome in {UpgradeOutcome.UPGRADED, UpgradeOutcome.UP_TO_DATE}
        assert env.files() == [exported.target]
        assert exported.target.read_bytes() != old_bytes
        assert [p.read_bytes() for p in bin_files(env)] == [old_bytes]
        assert record_of(env, exported).file_hash == digest(exported.target)
        staging = env.lib.library / ".shelfhold" / "staging"
        assert not any(p.is_file() for p in staging.rglob("*"))  # nothing left half-way
        with env.engine.connect() as connection:
            upgrades = connection.execute(
                sa.select(sa.func.count()).select_from(export.export_upgrade)
            ).scalar_one()
        assert upgrades == 1
