"""EF-SRC-032: the follow cycle asks a `ranged_content` service for whole volumes."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import FollowCycle
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition
from shelfhold.library.public import Received
from shelfhold.sdk.refs import RangedPayload, SeriesRef
from shelfhold.sdk.testing import FakeRangedConnectedService, fake_ranged_fetcher
from shelfhold.sdk.testing.fakes import FAKE_KEY
from tests.fakes_clock import ManualClock
from tests.follow_env import START, empty_catalog
from tests.integration.test_schema_catalog import add_edition, add_work
from tests.library_env import lib_env
from tests.null_tombstones import NONE_PURGED


class _Library(FakeRangedConnectedService):
    """The fake plugin with the one series it offers in the library of the service."""

    async def followed_series(self, f: Any) -> list[SeriesRef]:
        return [self._series()]


@pytest.mark.req("EF-SRC-032", "EF-SRC-034")
def test_follow_cycle_requests_and_files_the_volumes_of_a_ranged_service() -> None:
    kept: list[tuple[str, int]] = []

    @asynccontextmanager
    async def receive(asset: Any) -> Any:
        yield Received(
            path=Path("/nonexistent") / asset.filename, size=7, sha256="sha256:" + "0" * 64
        )

    def keep(received: Received, payload: RangedPayload, item_id: str) -> None:
        kept.append((item_id, len(payload.covers)))

    with lib_env() as lib:
        empty_catalog(lib)
        with lib.engine.begin() as connection:
            work_id = add_work(connection)
            edition_id = add_edition(connection, work_id)
            item_id, binding_id = new_ulid(), new_ulid()
            connection.execute(
                sa.insert(acquisition.source_item).values(
                    id=item_id,
                    plugin_id=FAKE_KEY,
                    source_key=FAKE_KEY,
                    remote_id="series/fake-novel",
                )
            )
            connection.execute(
                sa.insert(acquisition.source_binding).values(
                    id=binding_id, work_id=work_id, edition_id=edition_id,
                    source_item_id=item_id, priority=1, active=True,
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(acquisition.series_follow).values(edition_id=edition_id, work_id=work_id)
            )
        clock = ManualClock(START)
        cycle = FollowCycle(
            lib.engine, EventBus(clock), clock, _Library(), fake_ranged_fetcher(),
            store=None,  # type: ignore[arg-type]
            receive=receive, place=lambda *_: "unused", tombstones=NONE_PURGED, keep_range=keep,
        )  # fmt: skip
        for _ in range(4):  # discover, then queued, downloading, ready
            asyncio.run(cycle.run())
        with lib.engine.connect() as connection:
            items = connection.execute(sa.text("SELECT id, closed FROM export_item")).all()
            tickets = (
                connection.execute(sa.text("SELECT target_kind FROM download_request"))
                .scalars()
                .all()
            )
        assert set(tickets) == {"range"}
        assert sorted(n for _, n in kept) == [2, 8]  # Vol.1 whole, Vol.2 (rolling) whole
        assert {i.closed for i in items} == {0, 1} and len(items) == 2
