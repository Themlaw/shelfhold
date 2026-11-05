"""Shared pieces of the culpable benches: robust timing and a synthetic reference catalog.

Nothing here reaches the network. The time is the median of several runs, so one noisy sample on a
shared runner does not fail a bench, while a start that is really slow does.
"""

import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence

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


def time_process_start_to_ready(settings: Settings) -> float:
    """Seconds from spawning `shelfhold serve` to its first `/readyz` 200 (loopback only). The
    database is already migrated; each sample is a new process (import, boot and lock included)."""
    port = _free_port()
    env = {
        **os.environ,
        "SHELFHOLD_DATA_DIR": str(settings.data_dir),
        "SHELFHOLD_STORE_DIR": str(settings.store_dir),
        "SHELFHOLD_PORT": str(port),
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
                        return time.perf_counter() - started
            except (urllib.error.URLError, OSError):
                pass  # not listening yet, or 503 while not ready
            if process.poll() is not None:
                raise AssertionError(
                    f"server exited early: {process.stderr.read().decode()[-500:]}"
                )  # type: ignore[union-attr]
            if time.perf_counter() - started > GIVE_UP_S:
                raise AssertionError(f"/readyz still not ready after {GIVE_UP_S:.0f}s")
            time.sleep(POLL_INTERVAL_S)
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
