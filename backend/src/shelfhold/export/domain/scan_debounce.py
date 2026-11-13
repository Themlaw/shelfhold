"""Debounce of `scan-folder` requests: one scan per folder once writing is quiet (EF-EXP-009)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol


class _Clock(Protocol):
    def now(self) -> datetime: ...


DEFAULT_QUIET = timedelta(seconds=30)


class ScanDebouncer:
    """Collects folders; `take_due` returns them once nothing new came for `quiet`.

    A folder inside another requested folder is covered by it and dropped.
    """

    def __init__(self, clock: _Clock, quiet: timedelta = DEFAULT_QUIET) -> None:
        self._clock = clock
        self._quiet = quiet
        self._paths: set[str] = set()
        self._last: datetime | None = None

    def request(self, path: str) -> None:
        self._paths.add(path.rstrip("/") or "/")
        self._last = self._clock.now()

    def take_due(self) -> list[str]:
        if not self._paths or self._last is None or self._clock.now() - self._last < self._quiet:
            return []
        return self.take_all()

    def take_all(self) -> list[str]:
        """What was asked, now, without waiting for the quiet moment (the end of a batch)."""
        paths, self._paths, self._last = self._paths, set(), None
        kept = [
            p for p in paths if not any(p != o and p.startswith(o.rstrip("/") + "/") for o in paths)
        ]
        return sorted(kept)
