"""The disk guard (ENF-OPS-006): refuse writes under a free-space mark, resume above a higher one.

Free space under `max(5 %, 10 GiB)` pauses the write queues; they resume at `max(8 %, 15 GiB)`
(the absolute pause mark plus a fixed margin, so a volume under 125 GiB does not flap). Both marks
are settings. The guard measures and decides; it never deletes (the early purge of the trash is an
emitter of its own, EF-BIB-030).
"""

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3
DISK_LOW = "disk.low"


@dataclass(frozen=True)
class Usage:
    total: int
    free: int


def disk_usage(path: Path) -> Usage:
    stat = os.statvfs(path)  # free = what a non-root process may use (f_bavail)
    return Usage(total=stat.f_blocks * stat.f_frsize, free=stat.f_bavail * stat.f_frsize)


@dataclass(frozen=True)
class DiskThresholds:
    pause_percent: float = 5.0
    pause_gib: float = 10.0
    resume_percent: float = 8.0
    resume_gib: float = 15.0

    def __post_init__(self) -> None:
        # A resume mark equal to the pause mark would flap on every measure around it.
        if self.resume_percent <= self.pause_percent or self.resume_gib <= self.pause_gib:
            raise ValueError("the resume marks must be strictly above the pause marks")

    def pause_below(self, total: int) -> int:
        return int(max(total * self.pause_percent / 100, self.pause_gib * GIB))

    def resume_at(self, total: int) -> int:
        return int(max(total * self.resume_percent / 100, self.resume_gib * GIB))


class DiskLowError(OSError):
    """A write refused for lack of room; the same identifier as the event (EF-UX-003)."""

    code = DISK_LOW

    def __init__(self, volume: str) -> None:
        super().__init__(28, f"{DISK_LOW}: {volume} is under its free-space mark")
        self.volume = volume


@dataclass(frozen=True)
class DiskChange:
    volume: str
    low: bool  # True: it just went under the mark; False: it just recovered
    free: int
    total: int

    @property
    def free_percent(self) -> int:
        return int(self.free * 100 / self.total) if self.total else 0


@dataclass(frozen=True)
class DiskState:
    low: tuple[str, ...]
    changes: tuple[DiskChange, ...] = ()

    @property
    def paused(self) -> bool:
        return bool(self.low)


class DiskGuard:
    def __init__(
        self,
        volumes: Mapping[str, Path],
        *,
        thresholds: DiskThresholds | None = None,
        usage: Callable[[Path], Usage] = disk_usage,
        ttl_s: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._volumes = dict(volumes)
        self._thresholds = thresholds or DiskThresholds()
        self._usage = usage
        self._low: set[str] = set()
        self._ttl_s = ttl_s
        self._monotonic = monotonic
        self._measured_at: float | None = None
        self._unreported: list[DiskChange] = []  # transitions the watcher has not announced yet
        self._mutex = threading.Lock()

    def evaluate(self) -> DiskState:
        """Measure every volume once and hand over the transitions not yet announced."""
        with self._mutex:
            self._measure()
            changes, self._unreported = tuple(self._unreported), []
            return DiskState(tuple(sorted(self._low)), changes)

    def _measure(self) -> None:
        """The hysteresis lives in `_low`; transitions pile up in `_unreported`."""
        self._measured_at = self._monotonic()
        changes = self._unreported
        for name, path in self._volumes.items():
            try:
                usage = self._usage(path)
            except OSError:
                continue  # a mount that vanished is the sentinel's business, not a full disk
            was_low = name in self._low
            if was_low and usage.free >= self._thresholds.resume_at(usage.total):
                self._low.discard(name)
                changes.append(DiskChange(name, False, usage.free, usage.total))
            elif not was_low and usage.free < self._thresholds.pause_below(usage.total):
                self._low.add(name)
                changes.append(DiskChange(name, True, usage.free, usage.total))

    def low(self) -> tuple[str, ...]:
        return tuple(sorted(self._low))

    def require_space(self) -> None:
        """For a write that adds data: refused while any volume is paused. The measure is at most
        `ttl_s` old, taken here and not by a job (no consumer may be running)."""
        with self._mutex:
            if self._measured_at is None or self._monotonic() - self._measured_at >= self._ttl_s:
                self._measure()
        if self._low:
            raise DiskLowError(self.low()[0])

    def has_room_for_vacuum(self, *, database_bytes: int, volume: str) -> bool:
        """`VACUUM INTO` needs twice the size of the database free (ENF-OPS-006)."""
        return self._usage(self._volumes[volume]).free >= 2 * database_bytes
