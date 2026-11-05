"""EF-EXP-062 (I26): a merge then a split of Works never touches the exported series."""

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export
from tests.export_env import ExportEnv, Series, export_env

SLOW = settings(max_examples=12, deadline=None, suppress_health_check=list(HealthCheck))
ACTIONS = st.lists(st.sampled_from(["merge", "split", "chapter_a", "chapter_b", "run"]), max_size=7)


def _move(env: ExportEnv, edition_id: str, work_id: str) -> None:
    """What a merge or a split does to the catalog: the Edition and its bindings change Work."""
    with env.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == edition_id)
            .values(work_id=work_id)
        )
        connection.execute(
            sa.update(acquisition.source_binding)
            .where(acquisition.source_binding.c.edition_id == edition_id)
            .values(work_id=work_id)
        )


@pytest.mark.req("EF-EXP-062")
@given(actions=ACTIONS)
@SLOW
def test_merge_then_split_keeps_export_series_stable(actions: list[str]) -> None:
    with export_env() as env:
        a: Series = env.new_series("Monster", lang="fr")
        b: Series = env.new_series("Pluto", lang="en")
        numbers = {"a": 1, "b": 1}
        env.add_chapter(a, "2")
        env.add_chapter(b, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        series_before = {r.id: r.name_raw for r in env.rows(export.export_series)}
        links_before = {
            r.edition_id: r.export_series_id for r in env.rows(export.edition_export_series)
        }
        assert len(series_before) == 2
        for action in actions:
            if action == "merge":  # the Edition of Pluto joins the Work of Monster
                _move(env, b.edition_id, a.work_id)
            elif action == "split":
                _move(env, b.edition_id, b.work_id)
            elif action in ("chapter_a", "chapter_b"):
                side = a if action == "chapter_a" else b
                numbers[action[-1]] += 1
                env.add_chapter(side, str(numbers[action[-1]] + 1))
            else:
                plan = env.pipeline.run(env.destination_id, job_id=new_ulid())
                assert {op.kind for op in plan.ops} <= {"write"}  # never a rename
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        series_after = {r.id: r.name_raw for r in env.rows(export.export_series)}
        links_after = {
            r.edition_id: r.export_series_id for r in env.rows(export.edition_export_series)
        }
        assert series_after == series_before  # none orphaned, none duplicated, none renamed
        assert links_after == links_before
        for row in env.rows(export.export_record):
            assert row.state == "written"
