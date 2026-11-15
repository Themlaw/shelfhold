"""A simulated ComicVine search service replaying the cassettes of `cassettes/comicvine`."""

from __future__ import annotations

from tests.fakes.cassette_server import CASSETTES, CassetteServer

HOST = "comicvine.gamespot.com"
TITLE = "The Synthetic Chronicle"  # invented
HOSTILE = "Hostile Volume"  # an answer whose fields are not what they claim
ABSENT = "Nothing Recorded"
VOLUME_URL = "https://comicvine.gamespot.com/the-synthetic-chronicle/4050-777001/"


def fake_comicvine(name: str = "search", **options: object) -> CassetteServer:
    return CassetteServer(HOST, (CASSETTES / "comicvine" / f"{name}.json",), **options)  # type: ignore[arg-type]
