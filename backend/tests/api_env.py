"""Fixtures for the HTTP-conventions tests that do not go through `create_app()`."""

from collections.abc import Mapping
from datetime import UTC, datetime

T0 = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)


class Health:
    def migration_progress(self) -> tuple[int, int] | None:
        return None

    def readiness(self) -> Mapping[str, bool]:
        return {}
