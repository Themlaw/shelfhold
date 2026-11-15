"""A run that writes nothing must not make a stored plan stale. The arrival poll of such a run
confirms what Kavita indexed (`written` -> `scanned` -> `resolved`): the planner reads all three
alike (a file in the library), so the version a plan is stored under must not tell them apart."""

import pytest

from shelfhold.export.application.rename_plan import ItemMove
from shelfhold.export.application.snapshot import load_context, version_of
from shelfhold.export.application.target_state import load_health, save_health
from shelfhold.export.targets.kavita.polling import ArrivalPoller
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import export_env
from tests.kavita_env import run
from tests.kavita_export_env import REMOTE_ROOT, wire
from tests.rename_env import FakeReaders, export_chapters, item_ids_of, record_paths, tree


@pytest.mark.req("EF-EXP-037")
def test_a_noop_export_run_does_not_make_a_rename_plan_stale() -> None:
    with export_env() as env:
        w = wire(env)
        export_chapters(env, "Dr Stone", ["1"])
        run(w.client.probe())  # every export reads Kavita's version first (`_probe_version`)
        with write_transaction(env.engine) as connection:
            health = load_health(connection, env.target_id)
            seen = w.client.health
            health.version, health.version_mode = seen.version, seen.version_mode
            save_health(connection, env.target_id, health)
        env.pipeline.bind_readers(FakeReaders(count=0))
        moves = [
            ItemMove(i, f"Dr Stone (2019)/{n}") for n, i in item_ids_of(env, "Dr Stone").items()
        ]
        preview = env.pipeline.rename_preview(env.destination_id, moves=moves)

        # the periodic export finds nothing to write; its arrival poll sees Kavita index the files
        rels = sorted(r.rel_path for r in env.rows(export.export_record))
        folder = (REMOTE_ROOT + "/" + rels[0]).rsplit("/", 1)[0]
        w.fake.add_series("Dr Stone", folder, [w.remote(r) for r in rels])
        before = {r.state for r in env.rows(export.export_record)}
        poller = ArrivalPoller(env.engine, env.clock, w.events)
        run(poller.poll(w.client, env.destination_id))
        assert {r.state for r in env.rows(export.export_record)} != before  # the manifest moved

        with env.engine.connect() as connection:
            now = version_of(connection, load_context(connection, env.destination_id))
        assert now == preview.base_version  # nothing a plan depends on changed
        env.pipeline.apply(preview.plan_id, if_match=preview.base_version, job_id=new_ulid())
        assert record_paths(env) == tree(env)
