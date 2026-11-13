"""Counter-review PoC of #208 (M3 L11): the purge of a Work is a removal no `?since=` reports.

Decision 6 of L11 says no code deletes a Work row. `PurgeService.purge_work` does (EF-BIB-053
(4), `erase_work_rows`, M1b), and it writes no `ops.work_removed`; `erase_history` also erases
the earlier change events that cite the Work. A client holding a token from before the purge is
told nothing: the purged Work (its title in its cache) stays on the device of every reader who
saw it. Real purge, real removal reader. Red until the purge reports the removal (or forces a
resync of the tokens it outlives)."""

import pytest

from shelfhold.catalog.application import changes
from shelfhold.identity.domain.visibility import scope_of
from tests.family_env import family_env, rate
from tests.purge_env import ADMIN, make_purge_env

pytestmark = pytest.mark.req("EF-INT-001", "EF-BIB-053")


def test_counter_poc_purged_work_is_reported_as_removed() -> None:
    with family_env(13) as env:
        purge = make_purge_env(env.lib)
        series = env.new_series("Tale of the Quiet Harbour")
        env.add_chapter(series, "1")
        rate(env, series, 10)  # every reader sees it, a child of 13 included
        other = env.new_series("Tale of the Lantern Road")
        env.add_chapter(other, "1")
        rate(env, other, 12)  # the household goes on: the last event before the token is not hers
        with env.engine.connect() as connection:
            token = changes.window(connection).head  # a reader's token, before the purge
            marker = changes.history_marker(connection, token)
            assert changes.work_need(connection, series.work_id) < changes.NEED_NONE
        purge.purge.purge_work(
            series.work_id, principal=ADMIN, typed=series.work_id, now=purge.clock.now()
        )
        with env.engine.connect() as connection:
            assert changes.work_need(connection, series.work_id) == changes.NEED_NONE  # gone
            # what the route checks before it answers 200 (`catalog_lists._removals`)
            resync = (
                token < changes.window(connection).floor
                or changes.history_marker(connection, token) != marker
            )
            told = {
                who: changes.removed_ids(connection, scope_of(*scope), token)
                for who, scope in {"admin": ("admin", None), "kid": ("child", 13)}.items()
            }
        # either the token can no longer be answered (resync) or the removal is in the answer
        for who, removed in told.items():
            assert resync or series.work_id in removed, who
