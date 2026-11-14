"""Synthetic sources for the federated search (M3 L18): scripted pages, no network.

A `FakeSource` answers `pages` one after the other (its cursor is the index of the next page), can
fail, can be slow, and counts the calls it received. `FakeSearcher` is the one door the service
asks (`search(source_key, query, *, cursor)`), over a set of such sources.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from shelfhold.sdk.models import (
    API_VERSION,
    Cursor,
    Page,
    Permissions,
    PluginKind,
    RateLimit,
)
from shelfhold.sdk.refs import SeriesRef, UnitInfo
from shelfhold.sdk.source import Capability, SourceManifest
from tests.auth_env import AuthEnv, auth_env, db_rows

Hit = tuple[str, str, Mapping[str, str], Mapping[str, Any]]  # id, title, external ids, memo


def hit(
    remote_id: str, title: str, *, memo: Mapping[str, Any] | None = None, **external_ids: str
) -> Hit:
    """`memo`: what else the source says about the result (subjects, a cover, a summary...)."""
    return remote_id, title, external_ids, memo or {}


@dataclass
class FakeSource:
    key: str
    pages: Sequence[Sequence[Hit]] = ()
    kinds: tuple[str, ...] = ("text",)
    name: str | None = None
    error: Exception | None = None
    delay: float = 0.0
    units: Sequence[UnitInfo] = ()  # what `list_units` answers, for every series
    list_error: Exception | None = None  # `list_units` fails with it
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.manifest = SourceManifest(
            key=self.key, kind=PluginKind.SOURCE, version="1.0.0", api=API_VERSION,
            name=self.name or f"Source {self.key}", langs=["en"],
            content_kinds=set(self.kinds),  # type: ignore[arg-type]
            url_patterns=[], capabilities=Capability.SEARCH,
            rate_limit=RateLimit(requests_per_second=5.0, concurrency=2),
            permissions=Permissions(domains=[]),
        )  # fmt: skip

    def filters(self) -> list[Any]:
        return []

    async def list_units(self, f: Any, ref: SeriesRef) -> list[UnitInfo]:
        if self.list_error is not None:
            raise self.list_error
        return list(self.units)

    async def search(
        self, f: Any, q: str, filters: dict[str, Any], cursor: Cursor | None, *,
        sub: str | None = None,
    ) -> Page[SeriesRef]:  # fmt: skip
        return await self.answer(q, cursor.token if cursor else None)

    async def answer(self, q: str, cursor: str | None) -> Page[SeriesRef]:
        self.calls.append((q, cursor))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        index = int(cursor) if cursor else 0
        page = self.pages[index] if index < len(self.pages) else []
        refs = [
            SeriesRef(
                source_key=self.key, remote_id=remote_id, title=title,
                memo={**extra, **({"external_ids": dict(ids)} if ids else {})},
            )
            for remote_id, title, ids, extra in page
        ]  # fmt: skip
        more = index + 1 < len(self.pages)
        return Page[SeriesRef](items=refs, next=Cursor(token=str(index + 1)) if more else None)


class FakeSearcher:
    """What the service asks: one source by its key, one page."""

    def __init__(self, sources: Iterable[FakeSource]) -> None:
        self.sources = {s.key: s for s in sources}

    async def search(
        self, source_key: str, query: str, *, filters: Mapping[str, Any] | None = None,
        cursor: str | None = None,
    ) -> Page[SeriesRef]:  # fmt: skip
        return await self.sources[source_key].answer(query, cursor)

    @property
    def total_calls(self) -> int:
        return sum(len(s.calls) for s in self.sources.values())


# -- an instance with these sources behind the real route ---------------------------------------

MEMBER_ID = "01J0000000000000000000MEMB"
CHILD_ID = "01J0000000000000000000KKKK"


@contextmanager
def instance(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, *sources: FakeSource,
    active: Iterable[str] | None = None,
) -> Iterator[AuthEnv]:  # fmt: skip
    """`create_app()` as composed, with `sources` loaded as the host's own plugins (`active`: the
    keys that are switched on, all by default), a member and a child profile, and the admin."""
    import shelfhold.main as main_module

    registry = PluginRegistry(active_keys=None if active is None else set(active))
    registry.load_all(
        [
            PluginCandidate(key=s.key, origin=PluginOrigin.FIRST_PARTY, load=lambda s=s: s)  # type: ignore[misc]
            for s in sources
        ]
    )
    monkeypatch.setattr(main_module, "start_plugins", lambda *_a, **_k: (registry, []))
    with auth_env(monkeypatch, settings) as api:
        (admin_hash,) = db_rows(settings, "SELECT password_hash FROM user WHERE role = 'admin'")[0]
        connection = sqlite3.connect(settings.database_path)
        connection.execute("BEGIN IMMEDIATE")  # other writers (the app) are running
        try:
            connection.execute(
                "INSERT INTO user (id, username, password_hash, role, can_rate) "
                "VALUES (?, 'member', ?, 'member', 0)", (MEMBER_ID, admin_hash),
            )  # fmt: skip
            connection.execute(
                "INSERT INTO user (id, username, password_hash, role, can_rate, max_age_rating) "
                "VALUES (?, 'kid', ?, 'child', 0, 6)", (CHILD_ID, admin_hash),
            )  # fmt: skip
            connection.commit()
        finally:
            connection.close()
        yield api


def api_key_of(api: AuthEnv, username: str, *, read_only: bool = True) -> str:
    engine = create_db_engine(api.settings.database_path)
    try:
        return (
            local_auth(engine, api.settings)
            .create_api_key(name="k", username=username, read_only=read_only)
            .value
        )
    finally:
        engine.dispose()
