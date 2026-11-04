"""Shared harness of the Kavita client tests: simulated server, real fetcher, hand-moved clock."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher
from tests.fakes_clock import ManualClock

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
ROUTES_DOC = Path(__file__).resolve().parents[2] / "docs" / "kavita-routes.md"
OPENAPI_DIR = Path(__file__).resolve().parent / "fixtures" / "kavita" / "openapi"
_ROW = re.compile(r"^\|\s*(GET|POST|PUT|DELETE)\s*\|\s*`([^`]+)`")


def documented_routes() -> set[tuple[str, str]]:
    """The allow-list as `docs/kavita-routes.md` writes it."""
    rows = [_ROW.match(line) for line in ROUTES_DOC.read_text("utf-8").splitlines()]
    return {(m.group(1), m.group(2)) for m in rows if m}


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


class RecordingEvents:
    """The `TargetEvents` port: what the destination announces, in order."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    def emit(self, code: str, **detail: Any) -> None:
        self.emitted.append((code, detail))

    def codes(self) -> list[str]:
        return [code for code, _ in self.emitted]

    def details(self, code: str) -> list[dict[str, Any]]:
        return [detail for emitted, detail in self.emitted if emitted == code]


@dataclass
class Harness:
    fake: FakeKavita
    events: RecordingEvents
    clock: ManualClock
    client: KavitaClient
    extra: dict[str, Any] = field(default_factory=dict)


def harness(*, version: str = "0.9.1.4", **fake_options: Any) -> Harness:
    fake = FakeKavita(version=version, **fake_options)
    if fake.key_expires_at is None:
        fake.key_expires_at = NOW + timedelta(days=90)
    events, clock = RecordingEvents(), ManualClock(NOW)
    client = KavitaClient(
        build_fetcher(fake), BASE_URL, AdminKey(API_KEY), events=events, clock=clock
    )
    return Harness(fake, events, clock, client)
