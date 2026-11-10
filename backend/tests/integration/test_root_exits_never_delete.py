"""L25: whatever sequence of exits is played, no file disappears (EF-EXP-064, rule "aucune
suppression, sur aucun chemin")."""

import contextlib
import os

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from shelfhold.export.application.root_exits import RootExitError
from tests.purge_env import ADMIN
from tests.restore_env import restore_env
from tests.root_exits_env import FOREIGN_INSTANCE, SENTINEL, exits, root_id, tree, write_sentinel

DAMAGE = st.sampled_from(["lose", "foreign", "behind", "none"])
EXIT = st.sampled_from(["takeover", "reinit", "restored"])
STEP = st.tuples(DAMAGE, EXIT)


@pytest.mark.req("EF-EXP-064", "EF-INT-023")
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(steps=st.lists(STEP, min_size=1, max_size=5))
def test_no_exit_ever_deletes_a_file(steps: list[tuple[str, str]]) -> None:
    with restore_env() as env:
        ex, watcher = exits(env)
        rid = root_id(env)
        for damage, action in steps:
            if damage == "lose":
                if (env.library / SENTINEL).exists():
                    os.unlink(env.library / SENTINEL)
            elif damage == "foreign":
                write_sentinel(env.library, instance_id=FOREIGN_INSTANCE)
            elif damage == "behind":
                write_sentinel(env.library, generation=0)
            watcher.refresh()
            seen = tree(env.library)  # after the damage: only what an exit does is judged
            with contextlib.suppress(RootExitError):  # refused: nothing was asked to change
                getattr(ex, action)(rid, principal=ADMIN)
            now = tree(env.library)
            gone = sorted(k for k in seen if k not in now and k != "lease")
            assert not gone, f"{action} after {damage} removed {gone}"
            if action != "restored":
                assert damage == "none" or (env.library / SENTINEL).exists()
