"""Ports the export application and its target adapters share."""

from typing import Any, Protocol


class TargetEvents(Protocol):
    """Announces an operating event (`target.*`, events/operating.py); the detail is plain data."""

    def emit(self, code: str, **detail: Any) -> None: ...
