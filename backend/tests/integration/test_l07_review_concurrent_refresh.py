"""Relecture adverse de M6 L07: a refresh that read its record before another one applied.

`RepoStore.refresh` checks the version floor and the pinned key against the `RepoRecord` it was
handed, then awaits the network, then `_apply` writes unconditionally. Two refreshes that
overlap (two admin requests, the CLI and the API) therefore check against a stale floor: the
slower one writes an older index over a newer one, and puts back the key a rotation retired.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.plugin_host.index import PluginIndexError, RepoStore
from tests.plugin_index_env import (
    FakeFetcher,
    IndexKey,
    index_document,
    make_key,
    sign_index,
    trusted_comment,
)

pytestmark = pytest.mark.req("EF-SRC-017")

URL = "https://index.example.org/index.json"
T0 = datetime(2027, 1, 5, 9, 0, tzinfo=UTC)
EXPIRES = "2027-03-01T00:00:00Z"


@dataclass
class GatedFetcher(FakeFetcher):
    """A fetcher whose answers wait for the test: the slow side of the race."""

    gate: asyncio.Event = field(default_factory=asyncio.Event)

    async def get(self, url: str, **kwargs: Any) -> Any:
        await self.gate.wait()
        return await super().get(url, **kwargs)


def _serve(fetcher: FakeFetcher, key: IndexKey, version: int, **over: Any) -> bytes:
    document = index_document(version=version, expires=EXPIRES, **over)
    fetcher.serving(
        URL, document, sign_index(document, key, comment=trusted_comment(version, EXPIRES))
    )
    return document


def _store(settings: Settings) -> tuple[sa.Engine, RepoStore]:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    return engine, RepoStore(engine, now=lambda: T0)


def _record(store: RepoStore) -> Any:
    return next(r for r in store.list() if r.url == URL)


async def _race(store: RepoStore, slow: GatedFetcher, fast: FakeFetcher) -> Exception | None:
    slow_task = asyncio.create_task(store.refresh(_record(store), slow))  # reads the floor now
    await asyncio.sleep(0)
    await store.refresh(_record(store), fast)  # the newer index lands meanwhile
    slow.gate.set()
    try:
        await slow_task
    except PluginIndexError as exc:
        return exc
    return None


def test_overlapping_refresh_cannot_roll_the_floor_back(settings: Settings) -> None:
    engine, store = _store(settings)
    key = make_key()
    store.add(URL, key.public_b64)
    try:
        slow, fast = GatedFetcher(), FakeFetcher()
        _serve(slow, key, 3)  # an older index, well signed, held back by the network
        _serve(fast, key, 5)
        refused = asyncio.run(_race(store, slow, fast))
        assert store.list()[0].last_version == 5, "an older index overwrote a newer one"
        assert isinstance(refused, PluginIndexError)
    finally:
        engine.dispose()


def test_overlapping_refresh_cannot_undo_a_rotation(settings: Settings) -> None:
    engine, store = _store(settings)
    old, new = make_key(), make_key()
    store.add(URL, old.public_b64)
    try:
        slow = GatedFetcher()
        _serve(slow, old, 3)  # signed by the old key alone

        fast = FakeFetcher()
        rotating = _serve(fast, old, 2, next_pubkey=new.public_b64)
        fast.documents[URL + ".next.minisig"] = sign_index(
            rotating, new, comment=trusted_comment(2, EXPIRES)
        )
        refused = asyncio.run(_race(store, slow, fast))
        pinned = store.list()[0]
        assert pinned.key_id == new.key_id, "the retired key took the pin back after a rotation"
        assert isinstance(refused, PluginIndexError)
    finally:
        engine.dispose()
