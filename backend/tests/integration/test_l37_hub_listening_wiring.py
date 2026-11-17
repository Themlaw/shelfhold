"""M4 L37 — EF-EXP-034: the hub listeners, wired as the application wires them, on a real instance.

Only Kavita and its hub are synthetic. A series announced late by the hub gets the same follow-up
as one confirmed by an export (its cover), and with the hub down the export's own polling is what
it always was.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.infrastructure.schema import export
from tests.fakes.kavita.hub import ACK, FakeHub, frame
from tests.integration.test_l50_export import index_in_kavita, png, settle
from tests.l50_env import seeded

pytestmark = pytest.mark.req("EF-EXP-034")


async def _until(condition: object, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():  # type: ignore[operator]
            return True
        await asyncio.sleep(0.05)
    return False


def test_a_late_arrival_announced_by_the_hub_gets_its_cover(settings: Settings, tmp_path) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        for number in ("1", "2"):
            env.add_chapter(series, number)
        env.export()
        settle(env)  # the polling of the export: Kavita has not indexed the folder yet
        assert env.inst.kavita.covers == {}
        series_id = index_in_kavita(env)  # it does, later, and announces it on its hub
        hub = FakeHub([[ACK, frame("SeriesAdded", seriesId=series_id)]])
        listeners = rj.hub_listeners(env.inst.comp, active=lambda: True, connector=hub)

        async def scenario() -> bool:
            async with listeners.running(rescan_every=0.05):
                return await _until(lambda: env.inst.kavita.covers != {})

        assert asyncio.run(scenario())
        assert env.inst.kavita.covers == {series_id: png("1")}
        assert env.inst.kavita.violations == []
        record = env.rows(export.export_record)[0]
        assert record.state == "resolved" and record.remote_ref == str(series_id)


def test_listeners_do_nothing_when_the_instance_does_not_own_the_data(
    settings: Settings, tmp_path
) -> None:
    with seeded(settings, tmp_path) as env:
        hub = FakeHub([[ACK]])
        listeners = rj.hub_listeners(env.inst.comp, active=lambda: False, connector=hub)

        async def scenario() -> None:
            async with listeners.running(rescan_every=0.02):
                await asyncio.sleep(0.2)

        asyncio.run(scenario())
        assert hub.endpoints == [] and env.inst.kavita.hub_auth_calls == 0


def test_with_the_hub_down_the_export_polling_is_unchanged(settings: Settings, tmp_path) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.export()
        hub = FakeHub()  # every connection refused
        listeners = rj.hub_listeners(env.inst.comp, active=lambda: True, connector=hub)
        client = env.inst.comp.kavita_client(rj.tree_destinations(env.inst.comp)[0].target_id)

        async def scenario() -> bool:
            async with listeners.running(rescan_every=0.02):
                await _until(lambda: hub.endpoints != [])
                index_in_kavita(env)
                # the polling of the export confirms, whatever the hub does
                await rj.settle_arrival(env.inst.comp, client, env.destination_id)
                return True

        assert asyncio.run(scenario())
        assert hub.endpoints != []  # it did try
        assert env.rows(export.export_record)[0].state == "resolved"
