"""The step version of an apply (EF-EXP-042, ENF-PERF-007): the fence before every operation.

`inputs_version_of` is compared before every operation of an apply, so it must move for every
input a plan is made from — the facts of the Editions and the Units, the archives received for
them and the copies they are read from, the admissions of a shelf below 18+, the volumes — and
stand still for what the plan itself writes, or a plan would go stale because of its own
progress. It reads the sources as a superset (every accepted content, every library copy), so a
re-received chapter that did not become the preferred Variant moves it too: work refused, never
work lost.

The last test is the differential: a local copy of the derivation the fence used before the
scans (a full snapshot of the inputs through `_version`, the world left out) is kept here as
the reference, and every source of a moved input is checked against both — the old fence moves,
and the new one moves with it.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from shelfhold.export.application.snapshot import (
    DestinationContext,
    _inputs,
    _version,
    _volume_rows,
    admissions_of,
    inputs_version_of,
    load_context,
)
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, coherence, export, storage
from shelfhold.infrastructure.schema import replica as replicas
from shelfhold.library.public import CONTENT_HASH_BY
from tests.byte_source_env import Unit, add_format, drop_library_copy, unit_with_item
from tests.export_env import ExportEnv, Series, export_env, sha
from tests.family_env import family_env, rate_edition, suggest, unrate
from tests.integration.test_schema_catalog import add_variant

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


# -- the differential against the derivation the scans replaced ------------------------------


def old_inputs_version(connection: sa.Connection, ctx: DestinationContext) -> str:
    """The fence as it stood before the scans: a full snapshot of the inputs through `_version`
    (the world left out — the manifest and the series are what the plan itself writes), kept
    here as the reference the new fence must be at least as sensitive as."""
    editions, unit_rows, sources = _inputs(connection, ctx)
    return _version(
        ctx,
        editions,
        unit_rows,
        sources,
        None,
        admissions_of(connection, ctx, editions),
        _volume_rows(connection, editions),
    )


AGE_RATING = "an age rating on a family shelf"


def _series_above(env: ExportEnv, series: Series) -> str:
    """A Series above the Work, the Work a member of it (the Kavita series is the Series)."""
    series_id = new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(catalog.series).values(
                id=series_id, title="Cycle", sort_title="cycle", reading_order="publication"
            )
        )
        connection.execute(
            sa.insert(catalog.series_member).values(
                series_id=series_id, work_id=series.work_id, position="1.5", label="Tome 1.5"
            )
        )
    return series_id


def _second_variant(env: ExportEnv, series: Series, unit: Unit) -> str:
    """Another Variant of the Unit with a content of its own, not preferred (yet)."""
    with write_transaction(env.engine) as connection:
        variant = add_variant(connection, unit.unit_id, series.binding_id, "ch-alt")
        connection.execute(
            sa.insert(storage.content).values(
                id=new_ulid(), unit_id=unit.unit_id, variant_id=variant, kind="file",
                media_type="application/vnd.comicbook+zip", format="cbz",
                retention="passthrough", original_sha256=sha(b"alt received"),
                content_hash=sha(b"alt payload"), content_hash_by=CONTENT_HASH_BY,
                store_provenance="received", size=12,
            )
        )  # fmt: skip
    return variant


def _volume(env: ExportEnv, series: Series, unit: Unit) -> None:
    """A volume the keeper left over the Unit's chapter (EF-BIB-044)."""
    item = new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(export.export_item).values(
                id=item,
                edition_id=series.edition_id,
                kind="volume",
                closed=True,
                frozen_attrs={"volume_label": "Vol.1"},
            )
        )
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=item, unit_id=unit.unit_id,
                position=0, extent="full", origin="manual",
            )
        )  # fmt: skip


Change = Callable[[], object]


def _sources() -> dict[str, Callable[[ExportEnv, Series, Unit], list[Change]]]:
    """The changes of each source of a moved input, prepared before the first fence is taken
    (the Series, the second Variant, the admission the move takes away)."""

    def series_title(env: ExportEnv, series: Series, unit: Unit) -> list[Change]:
        above = _series_above(env, series)
        return [
            lambda: edit(env, catalog.series, {"title": "Cycle RENAMED"},
                        catalog.series.c.id == above)
        ]  # fmt: skip

    def added_to_a_series(env: ExportEnv, series: Series, unit: Unit) -> list[Change]:
        return [lambda: _series_above(env, series)]

    def edition_lang(env: ExportEnv, series: Series, unit: Unit) -> list[Change]:
        return [
            lambda: edit(env, catalog.edition, {"lang": "en"},
                        catalog.edition.c.id == series.edition_id)
        ]  # fmt: skip

    def edition_shelved_away_and_back(env: ExportEnv, series: Series, unit: Unit) -> list[Change]:
        return [
            lambda: edit(env, catalog.edition, {"shelf_id": None},
                        catalog.edition.c.id == series.edition_id),
            lambda: edit(env, catalog.edition, {"shelf_id": env.shelf_id},
                        catalog.edition.c.id == series.edition_id),
        ]  # fmt: skip

    def unit_field(column: sa.Column, value: object) -> Callable[..., list[Change]]:
        def build(env: ExportEnv, series: Series, unit: Unit) -> list[Change]:
            return [
                lambda: edit(
                    env, catalog.unit, {column.key: value}, catalog.unit.c.id == unit.unit_id
                )
            ]

        return build

    def content_hash(env: ExportEnv, series: Series, unit: Unit) -> list[Change]:
        return [
            lambda: edit(env, storage.content, {"content_hash": sha(b"another payload")},
                        storage.content.c.unit_id == unit.unit_id,
                        storage.content.c.format == "cbz")
        ]  # fmt: skip

    def preferred_variant(env: ExportEnv, series: Series, unit: Unit) -> list[Change]:
        other = _second_variant(env, series, unit)
        return [
            lambda: edit(env, catalog.unit_variant, {"preferred": True},
                        catalog.unit_variant.c.id == other)
        ]  # fmt: skip

    def damaged_copy(env: ExportEnv, series: Series, unit: Unit) -> list[Change]:
        return [
            lambda: edit(env, replicas.replica, {"damaged_at": DAMAGED_AT},
                        replicas.replica.c.unit_id == unit.unit_id,
                        replicas.replica.c.location == "library")
        ]  # fmt: skip

    def volume_kept(env: ExportEnv, series: Series, unit: Unit) -> list[Change]:
        return [lambda: _volume(env, series, unit)]

    def age_rating(env: ExportEnv, series: Series, unit: Unit) -> list[Change]:
        rate_edition(env, series.edition_id, 10)  # admitted before the fence is taken
        return [lambda: unrate(env, series.edition_id)]

    return {
        "series-title": series_title,
        "added-to-a-series": added_to_a_series,
        "edition-lang": edition_lang,
        "edition-shelved-away-and-back": edition_shelved_away_and_back,
        "unit-track": unit_field(catalog.unit.c.track, "extra"),
        "unit-part": unit_field(catalog.unit.c.part, "omake"),
        "unit-segment": unit_field(catalog.unit.c.number_segment, "s2"),
        "unit-source-order": unit_field(catalog.unit.c.source_order, 7),
        "content-hash": content_hash,
        "preferred-variant": preferred_variant,
        "damaged-copy": damaged_copy,
        "volume-kept": volume_kept,
        AGE_RATING: age_rating,
    }


@pytest.mark.req("ENF-PERF-007")
@pytest.mark.parametrize("source", sorted(_sources()))
def test_every_move_the_old_fence_saw_the_new_one_sees(source: str) -> None:
    """The differential: whatever moved the inputs for the old fence — the title of a Series or
    the Work joining one, the language, an Edition shelved away and back, the place of a Unit
    (track, part, segment, source order), the archive's own hash, a Variant becoming the
    preferred one, a copy damaged, a volume kept, the age rating of a family shelf — moves them
    for the scans too."""
    factory = family_env if source == AGE_RATING else export_env
    with factory() as env:
        series = env.new_series("Dr Stone")
        unit = unit_with_item(env, series, "2")
        changes = _sources()[source](env, series, unit)
        with env.engine.connect() as connection:
            ctx = load_context(connection, env.destination_id)
            before_old, before_new = (
                old_inputs_version(connection, ctx),
                inputs_version_of(connection, ctx),
            )
        for change in changes:
            change()
            with env.engine.connect() as connection:
                after_old, after_new = (
                    old_inputs_version(connection, ctx),
                    inputs_version_of(connection, ctx),
                )
            assert before_old != after_old, "the change moved no input the old fence read"
            assert before_new != after_new, "the scans missed what the old fence saw"
            before_old, before_new = after_old, after_new
