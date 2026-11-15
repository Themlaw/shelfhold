"""A simulated MangaUpdates API replaying the fabricated cassette of `cassettes/mangaupdates/`."""

from __future__ import annotations

from tests.fakes.cassette_server import CASSETTES, CassetteServer

HOST = "api.mangaupdates.com"
TITLE = "Where the Moon Never Reaches"  # invented
SERIES_ID = "700001"


def fake_mangaupdates(**options: object) -> CassetteServer:
    return CassetteServer(HOST, (CASSETTES / "mangaupdates" / "search.json",), **options)  # type: ignore[arg-type]
