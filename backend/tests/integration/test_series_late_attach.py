"""EF-BIB-025, EF-EXP-062: a Series above Works, attached late, moves only the exported link."""

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import Catalog, SeriesAlreadyAttachedError
from shelfhold.export.application.export_items import (
    FrozenSeriesError,
    reassign_edition_series,
)
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.domain.planner import SeriesRef
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.export_env import ExportEnv, Series, export_env

JOB = "01J00000000000000000000000"


def run(env: ExportEnv) -> None:
    env.pipeline.run(env.destination_id, job_id=new_ulid())


def tables(env: ExportEnv) -> dict[str, list[tuple[object, ...]]]:
    names = (export.export_series, export.export_item, export.export_record, catalog.unit)
    return {t.name: [tuple(r) for r in env.rows(t)] for t in names}


def attach(env: ExportEnv, title: str, members: list[Series]) -> str:
    with env.engine.begin() as connection:
        books = Catalog(connection)
        series_id = books.create_series(title)
        for position, member in enumerate(members, 1):
            books.attach_to_series(series_id, member.work_id, str(position))
    return series_id


def library_of(env: ExportEnv) -> str:
    with env.engine.connect() as connection:
        return str(load_context(connection, env.destination_id).remote_library_id)


@pytest.mark.req("EF-BIB-025")
def test_three_albums_export_as_one_kavita_series() -> None:
    with export_env() as env:
        albums = [env.new_series(f"Album {n}") for n in (1, 2, 3)]
        loner = env.new_series("Standalone")
        attach(env, "Le Cycle", albums)
        for number, member in enumerate(albums, 1):
            env.add_chapter(member, str(number))
        env.add_chapter(loner, "1")
        run(env)
        names = sorted(r.name_raw for r in env.rows(export.export_series))
        assert names == ["Le Cycle", "Standalone"]
        links = env.rows(export.edition_export_series)
        assert len(links) == 4 and len({r.export_series_id for r in links}) == 2
        folders = {p.parent.name for p in env.files() if p.suffix == ".cbz"}
        assert folders == {"Le Cycle", "Standalone"}


@pytest.mark.req("EF-BIB-025")
def test_late_attach_does_not_rename_a_series_already_exported() -> None:
    with export_env() as env:
        first, second = env.new_series("Album 1"), env.new_series("Album 2")
        env.add_chapter(first, "1")
        run(env)
        before = tables(env)
        attach(env, "Le Cycle", [first, second])
        env.add_chapter(second, "2")
        run(env)
        assert [r.name_raw for r in env.rows(export.export_series) if r.name_raw == "Album 1"]
        assert {r.name_raw for r in env.rows(export.export_series)} == {"Album 1", "Le Cycle"}
        assert all(row in tables(env)["export_series"] for row in before["export_series"])


@pytest.mark.req("EF-EXP-062")
def test_late_series_attach_changes_only_edition_export_series_link() -> None:
    with export_env() as env:
        first, second = env.new_series("Album 1"), env.new_series("Album 2")
        env.add_chapter(first, "1")
        env.add_chapter(second, "2")
        run(env)
        before = tables(env)
        old_link = {r.edition_id: r.export_series_id for r in env.rows(export.edition_export_series)}
        attach(env, "Le Cycle", [first, second])
        library = library_of(env)
        with env.engine.begin() as connection:
            target = reassign_edition_series(
                connection, edition_id=first.edition_id, remote_library_id=library,
                shelf_destination_id=env.destination_id,
                ref=SeriesRef(first.edition_id, "Le Cycle", "le cycle", proposal_key={"series": "s"}),
                now=env.clock.now(),
            )  # fmt: skip
            again = reassign_edition_series(
                connection, edition_id=second.edition_id, remote_library_id=library,
                shelf_destination_id=env.destination_id,
                ref=SeriesRef(second.edition_id, "Le Cycle", "le cycle", proposal_key={"series": "s"}),
                now=env.clock.now(),
            )  # fmt: skip
        assert target == again  # both albums now in one exported series
        after = tables(env)
        assert after["export_item"] == before["export_item"]  # no file, no name changed
        assert after["export_record"] == before["export_record"]
        assert after["unit"] == before["unit"]
        assert all(row in after["export_series"] for row in before["export_series"])  # none edited
        links = {r.edition_id: r.export_series_id for r in env.rows(export.edition_export_series)}
        assert links[first.edition_id] == links[second.edition_id] == target
        assert target not in old_link.values()
        assert {r.assigned_by for r in env.rows(export.edition_export_series)} == {"plan"}


@pytest.mark.req("EF-EXP-062")
def test_late_attach_never_renames_an_adopted_series() -> None:
    with export_env() as env:
        adopted = env.new_series("Old Name")
        env.add_chapter(adopted, "1")
        run(env)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(export.export_series).values(
                    frozen_series="Old Name", frozen_at=env.clock.now()
                )
            )
        before = tables(env)
        attach(env, "Le Cycle", [adopted])
        with pytest.raises(FrozenSeriesError), env.engine.begin() as connection:
            reassign_edition_series(
                connection, edition_id=adopted.edition_id, remote_library_id=library_of(env),
                shelf_destination_id=env.destination_id,
                ref=SeriesRef(adopted.edition_id, "Le Cycle", "le cycle"), now=env.clock.now(),
            )  # fmt: skip
        env.add_chapter(adopted, "2")
        run(env)  # the next chapter still goes where the adopted series is
        assert tables(env)["export_series"] == before["export_series"]
        assert len({p.parent for p in env.files() if p.suffix == ".cbz"}) == 1  # one folder


@pytest.mark.req("EF-BIB-025")
def test_a_work_belongs_to_one_series_at_most() -> None:
    with export_env() as env:
        lone = env.new_series("Album 1")
        first = attach(env, "Le Cycle", [lone])
        with env.engine.begin() as connection:
            books = Catalog(connection)
            other = books.create_series("Autre")
            with pytest.raises(SeriesAlreadyAttachedError):
                books.attach_to_series(other, lone.work_id, "1")
            assert books.series_of(lone.work_id) == first
