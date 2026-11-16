"""Synthetic stand-in for a Komga server (M4 L08a): nothing here comes from a third party."""

from tests.fakes.komga.server import (
    API_KEY,
    BASE_URL,
    OTHER_KEY,
    UNKNOWN_KEY,
    FakeKomga,
    RecordedRequest,
    administrator,
    build_fetcher,
    default_libraries,
    reader,
)

__all__ = [
    "API_KEY",
    "BASE_URL",
    "OTHER_KEY",
    "UNKNOWN_KEY",
    "FakeKomga",
    "RecordedRequest",
    "administrator",
    "build_fetcher",
    "default_libraries",
    "reader",
]
