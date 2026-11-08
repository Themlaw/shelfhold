"""Resident memory of a process and everything below it, read from `/proc` (Linux).

The server and its decode workers are different processes: the budget of ENF-PERF-001 is their
sum, so a child is never left out. `VmRSS` of each process is what the kernel keeps in RAM for it
(shared pages counted in each; an over-estimate, never an under-estimate).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024
_PROC = Path("/proc")


@dataclass(frozen=True, slots=True)
class TreeRss:
    total_bytes: int
    processes: int


def _children_map() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:  # gone while we scan
            continue
        # "pid (comm) S ppid ...": comm may hold spaces and parentheses, so cut at the last ")"
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        children.setdefault(ppid, []).append(int(entry.name))
    return children


def process_rss(pid: int) -> int:
    """`VmRSS` in bytes, 0 when the process is gone."""
    try:
        for line in (_PROC / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def tree_pids(root: int) -> list[int]:
    children = _children_map()
    found, todo = [], [root]
    while todo:
        pid = todo.pop()
        found.append(pid)
        todo.extend(children.get(pid, []))
    return found


def rss_tree(root: int) -> TreeRss:
    alive = [(pid, process_rss(pid)) for pid in tree_pids(root)]
    alive = [(pid, rss) for pid, rss in alive if rss > 0]
    return TreeRss(sum(rss for _, rss in alive), len(alive))


class PeakSampler:
    """Samples `rss_tree(root)` every `interval` seconds on a thread; `peak` is the maximum."""

    def __init__(self, root: int, interval: float = 0.02) -> None:
        self._root, self._interval = root, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.peak = TreeRss(0, 0)

    def _run(self) -> None:
        while not self._stop.is_set():
            sample = rss_tree(self._root)
            if sample.total_bytes > self.peak.total_bytes:
                self.peak = sample
            time.sleep(self._interval)

    def __enter__(self) -> PeakSampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(5)
