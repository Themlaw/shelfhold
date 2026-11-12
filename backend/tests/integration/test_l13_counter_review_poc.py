"""Counter-review PoCs of PR #201 (M3 L13), after the answers to the contradictor: two corrections
that do not hold everywhere. Red until fixed."""

from __future__ import annotations

import inspect
import sqlite3
from collections.abc import Iterator
from contextlib import closing

import pytest

from shelfhold.config import Settings
from shelfhold.jobs.kinds import traits_of
from shelfhold.store_jobs import STORE_INDEX_KIND, store_index_handler
from tests.auth_env import AuthEnv, auth_env

pytestmark = pytest.mark.req("EF-INT-017", "EF-USR-022")


def test_counter_poc_store_index_is_declared_cancellable_but_never_reads_stop() -> None:
    """M1 again, for one repair: `jobs.kinds` declares every repair `cancellable`, but the handler
    of `store.index` is one `rewrite()` that never reads `stop`. A cancel of a running index pass
    answers `cancelling`, the pass rewrites the whole index, and `_settle_cancel` records it
    `cancelled`: the ledger lies again, as M1 said. The trait must follow the handler."""
    reads_stop = "stop" in inspect.getsource(
        store_index_handler
    ) or "run_steps" in inspect.getsource(store_index_handler)
    assert traits_of(STORE_INDEX_KIND).cancellable == reads_stop


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def test_counter_poc_an_http_pause_keeps_its_scope_in_the_audit(api: AuthEnv) -> None:
    """The routes now pass `audit=False` to the service and leave the line to the guard, but the
    guard writes no target: through HTTP the audit no longer says which source was paused (or
    which job was cancelled, re-prioritised or replayed), while the same command by the host CLI
    does (`pause:<source>`). `reserved_action(..., audit=False)` on the route, with the service's
    line kept, is what `reserved_action` documents for a route that writes its own line."""
    api.login()
    answer = api.post("/api/v1/queue/pause", json={"source": "mangadex"})
    assert answer.status_code == 200, answer.text
    with closing(sqlite3.connect(api.settings.database_path)) as db:
        targets = [
            row[0] for row in db.execute("SELECT target FROM audit WHERE action = 'queue.control'")
        ]
    assert targets == ["pause:mangadex"], targets
