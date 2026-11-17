"""What the probes of the first pages share across the whole process (M4 L36, #341).

Every preview of an adoption probes the library through the decode worker, and a screen builds a
new `Adopter` for each request. So the verdicts, and the bound on how many workers run, live here,
once per process:

* a **verdict cache**, bounded, least recently used out: a file that did not change (path, size,
  mtime, `st_ctime_ns`, device and inode, none of which the mtime alone can forge) is not probed
  again by the next preview or by the adoption that follows it;
* a **slot count** shared by all previews and the CLI: at most `workers` decode workers at once,
  whoever asked (each one may use about 1.5 GiB of address space). Set from
  `SHELFHOLD_ADOPT_PROBE_WORKERS`.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

__all__ = [
    "CACHE_MAX",
    "DEFAULT_WORKERS",
    "PAGE_MAX_BYTES",
    "PageProbe",
    "PageVerdict",
    "clear",
    "configure",
    "lookup",
    "remember",
    "slot",
    "workers",
]

DEFAULT_WORKERS: Final = 2
PAGE_MAX_BYTES: Final = 32 * 1024 * 1024  # one page, in memory: the opener's own norm
CACHE_MAX: Final = 100_000


@dataclass(frozen=True, slots=True)
class PageProbe:
    """What the decode worker read of the first page of an adopted archive: format and size."""

    format: str
    width: int
    height: int


PageVerdict = tuple[PageProbe | None, str]  # the page's facts, and the closed code of a refusal
Stamp = tuple[int, ...]  # path-independent identity of the bytes: device, inode, size, mtime, ctime

_lock = threading.Lock()
_workers = DEFAULT_WORKERS
_slots = threading.BoundedSemaphore(DEFAULT_WORKERS)
_cache: OrderedDict[tuple[str, Stamp], PageVerdict] = OrderedDict()


def configure(count: int) -> None:
    """The most workers at once, for the whole process (a call while probes run lets those end
    on the count they started with)."""
    global _workers, _slots
    if count < 1:
        raise ValueError("at least one probe worker")
    with _lock:
        _workers, _slots = count, threading.BoundedSemaphore(count)


def workers() -> int:
    with _lock:
        return _workers


@contextmanager
def slot() -> Iterator[None]:
    """Hold one of the process's probe slots for the time of a read and a probe."""
    with _lock:
        held = _slots
    held.acquire()
    try:
        yield
    finally:
        held.release()


def lookup(path: str, stamp: Stamp) -> PageVerdict | None:
    with _lock:
        verdict = _cache.get((path, stamp))
        if verdict is not None:
            _cache.move_to_end((path, stamp))
        return verdict


def remember(path: str, stamp: Stamp, verdict: PageVerdict) -> None:
    with _lock:
        _cache[(path, stamp)] = verdict
        _cache.move_to_end((path, stamp))
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def clear() -> None:
    with _lock:
        _cache.clear()
