"""Shared pieces of the culpable benches: robust timing and a synthetic reference catalog.

Nothing here reaches the network. The time is the median of several runs, so one noisy sample on a
shared runner does not fail a bench, while a start that is really slow does.
"""

import json
import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog

READYZ_BUDGET_S = 15.0  # ENF-PERF-003: `/readyz` < 15 s outside migration time
RUNS = 5  # odd, so the median is a measured sample
POLL_INTERVAL_S = 0.05
GIVE_UP_S = 60.0  # a start that never answers fails the bench instead of hanging the job

SERVE = "from shelfhold.interfaces.cli import main; raise SystemExit(main(['serve']))"
WORKS = 500
UNITS_PER_WORK = 40  # 20 000 Units: a small library, enough for every start-up scan to have work


def median_seconds(samples: Sequence[float]) -> float:
    if not samples:
        raise ValueError("no sample")
    return statistics.median(samples)


def seed_reference_catalog(settings: Settings) -> None:
    """Works, Editions and Units written in bulk: synthetic titles, no file, no source."""
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.insert(catalog.work_kind)
                .values(key="manga", label_fr="manga", default_media="images")
                .prefix_with("OR IGNORE")
            )
            works = [
                {"id": new_ulid(), "kind": "manga", "title": f"Work {n}", "sort_title": f"work {n}"}
                for n in range(WORKS)
            ]
            connection.execute(sa.insert(catalog.work), works)
            editions = [
                {
                    "id": new_ulid(),
                    "work_id": w["id"],
                    "lang": "fr",
                    "translation_type": "official",
                    "media": "images",
                }
                for w in works
            ]
            connection.execute(sa.insert(catalog.edition), editions)
            units = [
                {
                    "id": new_ulid(),
                    "edition_id": e["id"],
                    "number_raw": str(n),
                    "number_norm": str(n),
                    "number_sort": float(n),
                    "track": "main",
                    "kind": "chapter",
                }
                for e in editions
                for n in range(1, UNITS_PER_WORK + 1)
            ]
            connection.execute(sa.insert(catalog.unit), units)
    finally:
        engine.dispose()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass(frozen=True, slots=True)
class Server:
    pid: int
    port: int
    ready_after_s: float  # from the spawn to the first `/readyz` 200


@contextmanager
def running_server(settings: Settings, *, settle_s: float = 0.0) -> Iterator[Server]:
    """A real `shelfhold serve` on a free loopback port, ready (`/readyz` 200), then left alone
    for `settle_s` seconds. The database is already migrated. Only this process is ever stopped."""
    port = _free_port()
    env = {
        **os.environ,
        "SHELFHOLD_DATA_DIR": str(settings.data_dir),
        "SHELFHOLD_STORE_DIR": str(settings.store_dir),
        "SHELFHOLD_PORT": str(port),
        "MALLOC_ARENA_MAX": "2",  # as in the image (ENF-PERF-001)
    }
    started = time.perf_counter()
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", SERVE],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )  # fmt: skip
    try:
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as reply:
                    if reply.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                pass  # not listening yet, or 503 while not ready
            if process.poll() is not None:
                raise AssertionError(
                    f"server exited early: {process.stderr.read().decode()[-500:]}"
                )  # type: ignore[union-attr]
            if time.perf_counter() - started > GIVE_UP_S:
                raise AssertionError(f"/readyz still not ready after {GIVE_UP_S:.0f}s")
            time.sleep(POLL_INTERVAL_S)
        ready_after = time.perf_counter() - started
        time.sleep(settle_s)
        yield Server(process.pid, port, ready_after)
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def time_process_start_to_ready(settings: Settings) -> float:
    """Seconds from spawning `shelfhold serve` to its first `/readyz` 200 (loopback only); each
    sample is a new process (import, boot and lock included)."""
    with running_server(settings) as server:
        return server.ready_after_s


REFERENCE = Path(__file__).resolve().parents[3] / ".github" / "bench-reference.json"


def load_reference() -> dict[str, Any]:
    """The versioned reference of the benches: thresholds of the spec and the profile's manifest."""
    return cast("dict[str, Any]", json.loads(REFERENCE.read_text(encoding="utf-8")))


BACKEND = Path(__file__).resolve().parents[2]
EXPORT_GIVE_UP_S = 25 * 60


@dataclass(frozen=True, slots=True)
class ExportPeak:
    tree_bytes: int  # highest sum of VmRSS over the driver and its children, sampled from outside
    processes: int  # how many processes made up that peak (the decode workers are among them)
    own_high_water_bytes: int  # VmHWM of the driver itself, from the kernel
    ops: int  # files the export wrote


def export_peak(*, chapters: int, decodes: int, profile: bool) -> ExportPeak:
    """Run `tests.bench.export_driver` and report the peak resident memory of its process tree."""
    from tests.bench.rss import PeakSampler

    command = [sys.executable, "-m", "tests.bench.export_driver", str(chapters), str(decodes)]
    if profile:
        command.append("--profile")
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        command,
        cwd=BACKEND, env={**os.environ, "MALLOC_ARENA_MAX": "2"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    with PeakSampler(process.pid) as sampler:
        try:
            out, err = process.communicate(timeout=EXPORT_GIVE_UP_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    if process.returncode != 0:
        raise AssertionError(f"export driver failed ({process.returncode}): {err[-800:]}")
    report = json.loads(out.strip().splitlines()[-1])
    return ExportPeak(
        sampler.peak.total_bytes, sampler.peak.processes, report["own_high_water_bytes"],
        report["ops"],
    )  # fmt: skip
