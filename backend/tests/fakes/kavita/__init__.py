"""Synthetic stand-in for a Kavita server (L25): nothing here comes from a third party."""

from tests.fakes.kavita.server import (
    API_KEY,
    BASE_URL,
    FakeKavita,
    RecordedRequest,
    build_fetcher,
)

__all__ = ["API_KEY", "BASE_URL", "FakeKavita", "RecordedRequest", "build_fetcher"]
