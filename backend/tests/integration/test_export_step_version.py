"""The step version of an apply (EF-EXP-042, ENF-PERF-007): the fence before every operation.

`inputs_version_of` is compared before every operation of an apply, so it must move for every
input a plan is made from — the facts of the Editions and the Units, the archives received for
them and the copies they are read from, the admissions of a shelf below 18+, the volumes — and
stand still for what the plan itself writes, or a plan would go stale because of its own
progress. It reads the sources as a superset (every accepted content, every library copy), so a
re-received chapter that did not become the preferred Variant moves it too: work refused, never
work lost.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from shelfhold.export.application.snapshot import inputs_version_of, load_context
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, coherence, export, storage
from shelfhold.infrastructure.schema import replica as replicas
from tests.byte_source_env import add_format, drop_library_copy, unit_with_item
from tests.export_env import ExportEnv, export_env
from tests.family_env import family_env, rate_edition, suggest, unrate

DAMAGED_AT = datetime(2026, 11, 23, tzinfo=UTC)


def version_of_inputs(env: ExportEnv) -> str:
    with env.engine.connect() as connection:
        return inputs_version_of(connection, load_context(connection, env.destination_id))


def moves(env: ExportEnv, change: Callable[[], object]) -> None:
    """The step version before `change` differs from the one after it."""
    before = version_of_inputs(env)
    change()
    assert version_of_inputs(env) != before, "the change moved no input the fence reads"


def edit(env: ExportEnv, table: sa.Table, values: dict[str, object], *where: object) -> None:
    """The mutation of one row, as an admin's edit or a migration would leave it."""
    with write_transaction(env.engine) as connection:
        connection.execute(sa.update(table).where(*where).values(**values))


@pytest.fixture
def held() -> tuple[ExportEnv, object, object]:
    with export_env() as env:
        series = env.new_series("Dr Stone")
        unit = unit_with_item(env, series, "2")
        yield env, series, unit


@pytest.mark.req("ENF-PERF-007")
def test_the_fence_covers_the_facts_of_the_editions(held) -> None:
    env, series, _unit = held
    moves(
        env,
        lambda: edit(
            env, catalog.work, {"title": "Dr Stone RENAMED"}, catalog.work.c.id == series.work_id
        ),
    )
    moves(
        env,
        lambda: edit(
            env,
            catalog.edition,
            {"packaging": "kanzenban"},
            catalog.edition.c.id == series.edition_id,
        ),
    )


@pytest.mark.req("ENF-PERF-007")
def test_the_fence_covers_the_units_of_the_shelf(held) -> None:
    env, series, unit = held
    moves(env, lambda: env.add_chapter(series, "3"))
    moves(
        env,
        lambda: edit(env, catalog.unit, {"number_norm": "2.5"}, catalog.unit.c.id == unit.unit_id),
    )
    other = env.lib.new_unit()  # a unit of another shelf: no plan of this one is made from it
    _unchanged(
        env,
        lambda: edit(env, catalog.unit, {"number_norm": "8.8"}, catalog.unit.c.id == other[0]),
    )


def _unchanged(env: ExportEnv, change: Callable[[], object]) -> None:
    """What no plan of this shelf is made from: the fence stands still for it."""
    before = version_of_inputs(env)
    change()
    assert version_of_inputs(env) == before, "a change outside the shelf moved the fence"


@pytest.mark.req("ENF-PERF-007")
def test_the_fence_covers_the_archives_received(held) -> None:
    env, _series, unit = held
    moves(
        env,
        lambda: edit(
            env,
            storage.content,
            {"watermarked": True},
            storage.content.c.unit_id == unit.unit_id,
            storage.content.c.format == "cbz",
        ),
    )
    moves(env, lambda: add_format(env, unit, "epub", b"a second content"))


@pytest.mark.req("ENF-PERF-007")
def test_the_fence_covers_the_copies_the_sources_are_read_from(held) -> None:
    """The copy a chapter is served from: damaged (the fence moves, the plan stops before it
    reads a broken file), then gone."""
    env, _series, unit = held
    moves(
        env,
        lambda: edit(
            env,
            replicas.replica,
            {"damaged_at": DAMAGED_AT},
            replicas.replica.c.unit_id == unit.unit_id,
            replicas.replica.c.location == "library",
        ),
    )
    moves(env, lambda: drop_library_copy(env, unit))


@pytest.mark.req("ENF-PERF-007")
def test_the_fence_covers_the_admissions_of_a_family_shelf() -> None:
    """A rating that moves stales the plan (EF-USR-015): the fence reads the verdicts through
    the same service the planner does, over the Editions its own scan found."""
    with family_env() as env:
        series = env.new_series()
        unit_with_item(env, series, "1")
        rate_edition(env, series.edition_id, 10)  # admitted: the shelf's own verdict for it
        moves(env, lambda: unrate(env, series.edition_id))
        rate_edition(env, series.edition_id, 10)  # admitted again
        moves(env, lambda: suggest(env, series.edition_id, 18))


@pytest.mark.req("ENF-PERF-007")
def test_the_fence_stands_still_for_what_the_plan_itself_writes(held) -> None:
    """A plan must not go stale because of its own progress: the fence reads none of the tables
    an operation of the plan writes (the manifest, an intent, the locks of the operations)."""
    env, _series, unit = held
    before = version_of_inputs(env)
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(export.export_record).values(
                shelf_destination_id=env.destination_id, export_item_id=unit.item_id,
                locator_type="path", state="planned", rel_path="Dr Stone/Ch.0002.cbz",
                file_hash="sha256:" + "0" * 64,
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(coherence.fs_intent).values(
                id=new_ulid(), op="write", intent_schema_version="1",
                dst_path="/somewhere/Ch.0002.cbz", state="pending", epoch=0,
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(coherence.resource_lock).values(
                resource="path:somewhere/Dr Stone", holder_job_id=new_ulid(),
                lease_until=env.clock.now(),
            )
        )  # fmt: skip
    assert version_of_inputs(env) == before


@pytest.mark.req("ENF-PERF-007")
def test_the_fence_stands_still_when_nothing_moved(held) -> None:
    """The same inputs give the same fence twice: a plan is not refused at random."""
    env, _series, _unit = held
    assert version_of_inputs(env) == version_of_inputs(env)
