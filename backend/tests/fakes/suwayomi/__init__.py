"""Synthetic stand-in for a Suwayomi server (L17): nothing here comes from a third party."""

from tests.fakes.suwayomi.server import (
    BASE_URL,
    PASSWORD,
    USER,
    FakeSuwayomi,
    FakeSuwayomiFetcher,
    RecordedRequest,
)

__all__ = [
    "BASE_URL",
    "PASSWORD",
    "USER",
    "FakeSuwayomi",
    "FakeSuwayomiFetcher",
    "RecordedRequest",
]
