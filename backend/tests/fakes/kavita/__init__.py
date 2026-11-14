"""Synthetic stand-in for a Kavita server (L25): nothing here comes from a third party."""

from tests.fakes.kavita.server import (
    API_KEY,
    BASE_URL,
    OTHER_KEY,
    FakeKavita,
    RecordedRequest,
    build_fetcher,
    default_libraries,
    member,
)

__all__ = [
    "API_KEY",
    "BASE_URL",
    "OTHER_KEY",
    "FakeKavita",
    "RecordedRequest",
    "build_fetcher",
    "default_libraries",
    "member",
]
