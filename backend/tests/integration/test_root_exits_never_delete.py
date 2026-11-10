"""L25: whatever sequence of exits is played, no file disappears and none changes
(EF-EXP-064, rule "aucune suppression, sur aucun chemin")."""

import contextlib
import hashlib
import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from shelfhold.export.application.root_exits import RootExitError
from tests.purge_env import ADMIN
from tests.restore_env import restore_env
from tests.root_exits_env import FOREIGN_INSTANCE, SENTINEL, exits, root_id, write_sentinel

DAMAGE = st.sampled_from(["lose", "foreign", "behind", "none", "unmount", "modify"])
EXIT = st.sampled_from(["takeover", "reinit", "restored", "store_reinit"])
STEP = st.tuples(DAMAGE, EXIT, st.booleans())  # (what happens, which exit, keyring or not)
VOLATILE = {SENTINEL, "lease"}


def digests(*roots: Path) -> dict[str, str]:
    """Every file of every root, `.shelfhold/` included, by the hash of its content."""
    found: dict[str, str] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.name not in VOLATILE:
                found[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


@pytest.mark.req("EF-EXP-064", "EF-INT-023")
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(steps=st.lists(STEP, min_size=1, max_size=5))
def test_no_exit_ever_deletes_or_changes_a_file(steps: list[tuple[str, str, bool]]) -> None:
    with restore_env() as env:
        store = env.library.parent / "kept-store"
        store.mkdir()
        (store / "kept.cbz").write_bytes(b"kept copy")
        (store / ".shelfhold").mkdir()
        (store / ".shelfhold" / "note").write_bytes(b"host file")
        rid = root_id(env)
        for damage, action, keyring in steps:
            ex, watcher = exits(env, keyring=keyring, store_path=str(store))
            if damage == "lose":
                (env.library / SENTINEL).unlink(missing_ok=True)
            elif damage == "foreign":
                write_sentinel(env.library, instance_id=FOREIGN_INSTANCE)
            elif damage == "behind":
                write_sentinel(env.library, generation=0)
            elif damage == "unmount":  # the volume goes away and comes back
                away = env.library.with_name("library.away")
                os.rename(env.library, away)
                os.rename(away, env.library)
            elif damage == "modify":  # a third party edits a user file
                target = env.managed()[0]
                target.write_bytes(target.read_bytes() + b" edited")
            with contextlib.suppress(Exception):  # a root that cannot be judged: nothing to do
                watcher.refresh()
            before = digests(env.library, store)
            with contextlib.suppress(RootExitError, PermissionError):  # refused: nothing changed
                if action == "store_reinit":
                    ex.store_reinit(principal=ADMIN)
                else:
                    getattr(ex, action)(rid, principal=ADMIN)
            after = digests(env.library, store)
            gone = sorted(k for k in before if k not in after)
            changed = sorted(k for k in before if k in after and before[k] != after[k])
            assert not gone, f"{action} after {damage} removed {gone}"
            assert not changed, f"{action} after {damage} changed {changed}"
