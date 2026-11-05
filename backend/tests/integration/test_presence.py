"""EF-BIB-044 (I20): a Unit is present for acquisition, and present in a destination."""

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import present_for_acquisition, present_in
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import record_replica
from tests.export_env import add_second_shelf, export_env
from tests.integration.test_schema_catalog import add_unit


@pytest.mark.req("EF-BIB-044")
def test_presence_predicates_acquisition_vs_destination() -> None:
    with export_env() as env:
        series = env.new_series()
        exported = env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        _shelf, second = add_second_shelf(env)  # another destination, nothing written there
        volume = _adopted_volume(env, series.edition_id, ["4", "5"])
        with env.engine.begin() as connection:
            bare = add_unit(connection, series.edition_id, "3")  # known, no file anywhere
            _record(connection, volume.item, env.destination_id, "adopted")

        with env.engine.connect() as connection:
            assert present_for_acquisition(connection, exported)
            assert present_in(connection, exported, env.destination_id)
            assert not present_in(connection, exported, second)  # per destination, not global

            assert not present_for_acquisition(connection, bare)
            assert not present_in(connection, bare, env.destination_id)

            # a volume Kavita already shows: present for acquisition and in that destination,
            # absent from the second one
            for unit in volume.units:
                assert present_for_acquisition(connection, unit)
                assert present_in(connection, unit, env.destination_id)
                assert not present_in(connection, unit, second)


@pytest.mark.req("EF-BIB-044")
def test_a_missing_or_recycled_record_is_not_presence() -> None:
    with export_env() as env:
        series = env.new_series()
        unit = env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.export_record).values(state="missing"))
        with env.engine.connect() as connection:
            assert present_for_acquisition(connection, unit)  # the readable copy is still there
            assert not present_in(connection, unit, env.destination_id)


class _Volume:
    def __init__(self, item: str, units: list[str]) -> None:
        self.item, self.units = item, units


def _adopted_volume(env: object, edition_id: str, numbers: list[str]) -> _Volume:
    connection_source = env.engine  # type: ignore[attr-defined]
    item = new_ulid()
    with connection_source.begin() as connection:
        units = [add_unit(connection, edition_id, n) for n in numbers]
        connection.execute(
            sa.insert(export.export_item).values(id=item, edition_id=edition_id, kind="adopted")
        )
        for position, unit in enumerate(units):
            connection.execute(
                sa.insert(export.unit_coverage).values(
                    carrier_type="export_item", carrier_id=item, unit_id=unit, position=position,
                    extent="full", origin="manual",
                )
            )  # fmt: skip
        record_replica(
            connection, unit_id=units[0], location="adopted_ro", content_hash=None,
            independent=True, physical_root_id=env.lib.library_root,  # type: ignore[attr-defined]
            rel_path="vol-01.cbz", export_item_id=item,
        )  # fmt: skip
    return _Volume(item, units)


def _record(connection: sa.Connection, item: str, destination: str, state: str) -> None:
    connection.execute(
        sa.insert(export.export_record).values(
            shelf_destination_id=destination, export_item_id=item, locator_type="path",
            rel_path="Series/vol-01.cbz", state=state,
        )
    )  # fmt: skip
