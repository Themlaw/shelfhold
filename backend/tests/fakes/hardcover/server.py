"""A simulated Hardcover GraphQL service replaying the fabricated cassettes of `cassettes/hardcover`."""

from __future__ import annotations

from tests.fakes.cassette_server import CASSETTES, CassetteServer

HOST = "api.hardcover.app"
TITLE = "The Synthetic Chronicle"  # invented
ISBN = "9780000000002"  # invented
HOSTILE = "Hostile Volume"  # an answer whose fields are not what they claim
ABSENT = "Nothing Recorded"
BOOK_URL = "https://hardcover.app/books/the-synthetic-chronicle"


def fake_hardcover(name: str = "search", **options: object) -> CassetteServer:
    return CassetteServer(HOST, (CASSETTES / "hardcover" / f"{name}.json",), **options)  # type: ignore[arg-type]
