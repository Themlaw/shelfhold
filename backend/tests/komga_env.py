"""Shared harness of the Komga client tests: simulated server, real fetcher, hand-moved clock."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from shelfhold.export.targets.komga import KomgaClient, KomgaKey
from tests.fakes.komga import API_KEY, BASE_URL, FakeKomga, build_fetcher
from tests.fakes_clock import ManualClock

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
ROUTES_DOC = Path(__file__).resolve().parents[2] / "docs" / "destinations" / "komga-routes.md"
_ROW = re.compile(r"^\|\s*(GET|POST|PUT|DELETE)\s*\|\s*`([^`]+)`")


def documented_routes() -> set[tuple[str, str]]:
    """The allow-list as `docs/destinations/komga-routes.md` writes it."""
    rows = [_ROW.match(line) for line in ROUTES_DOC.read_text("utf-8").splitlines()]
    return {(m.group(1), m.group(2)) for m in rows if m}


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


@dataclass
class Harness:
    fake: FakeKomga
    clock: ManualClock
    client: KomgaClient


def harness(*, key: str = API_KEY, **fake_options: object) -> Harness:
    fake = FakeKomga(**fake_options)  # type: ignore[arg-type]
    clock = ManualClock(NOW)
    client = KomgaClient(build_fetcher(fake), BASE_URL, KomgaKey(key), clock=clock)
    return Harness(fake, clock, client)
