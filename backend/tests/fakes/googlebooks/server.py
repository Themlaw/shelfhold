"""A simulated Google Books service replaying the fabricated cassettes of `cassettes/googlebooks`."""

from __future__ import annotations

from tests.fakes.cassette_server import CASSETTES, CassetteServer

HOST = "www.googleapis.com"
TITLE = "The Synthetic Chronicle"  # invented
ISBN = "9780000000002"  # invented
HOSTILE = "Hostile Volume"
ABSENT = "Nothing Recorded"
VOLUME_URL = "https://books.google.com/books?id=invAAAAAAAAA&source=gbs_api"


def fake_googlebooks(name: str = "volumes", **options: object) -> CassetteServer:
    return CassetteServer(HOST, (CASSETTES / "googlebooks" / f"{name}.json",), **options)  # type: ignore[arg-type]
