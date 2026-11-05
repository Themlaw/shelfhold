"""A deterministic fault simulator for the failure matrix (ENF-OPS-011).

Nothing here sleeps or reads the real clock: a fault is a scripted behaviour of a dependency that
the production components then meet, so a CI run replays the same matrix every time.
"""

from pathlib import Path

from shelfhold.infrastructure.fetcher.errors import FetchError
from shelfhold.infrastructure.observability.disk_guard import Usage

GIB = 1024**3


class FakeVolume:
    """`usage` of a volume of `total_gib` with `free_gib` left; move `free_gib` to inject a fill."""

    def __init__(self, total_gib: float, free_gib: float) -> None:
        self.total_gib = total_gib
        self.free_gib = free_gib

    def __call__(self, _path: Path) -> Usage:
        return Usage(total=int(self.total_gib * GIB), free=int(self.free_gib * GIB))


class DownFetcher:
    """The Fetcher seen by a connected service that does not answer: every call fails."""

    def __init__(self) -> None:
        self.calls = 0
        self.up = False

    async def get(self, url: str, **_options: object) -> object:
        self.calls += 1
        raise FetchError(f"{url} does not answer")

    post = get


class FaultSimulator:
    def volume(self, total_gib: float = 100, free_gib: float = 50) -> FakeVolume:
        return FakeVolume(total_gib, free_gib)

    def dead_fetcher(self) -> DownFetcher:
        return DownFetcher()
