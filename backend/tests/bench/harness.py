"""Shared pieces of the culpable benches: robust timing and a synthetic reference catalog.

Nothing here reaches the network. The time is the median of several runs, so one noisy sample on a
shared runner does not fail a bench, while a start that is really slow does.
"""

import statistics
import time
from collections.abc import Sequence

import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from shelfhold.main import create_app

READYZ_BUDGET_S = 15.0  # ENF-PERF-003: `/readyz` < 15 s outside migration time
RUNS = 5  # odd, so the median is a measured sample
POLL_INTERVAL_S = 0.05
GIVE_UP_S = 60.0  # a start that never answers fails the bench instead of hanging the job

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


def time_cold_start_to_ready(settings: Settings) -> float:
    """Seconds from building the application to the first `/readyz` 200 (a new process state: a
    fresh application, lifespan and engine each time; the database is already migrated)."""
    started = time.perf_counter()
    with TestClient(create_app(settings)) as client:
        while True:
            if client.get("/readyz").status_code == 200:
                return time.perf_counter() - started
            if time.perf_counter() - started > GIVE_UP_S:
                raise AssertionError(f"/readyz still not ready after {GIVE_UP_S:.0f}s")
            time.sleep(POLL_INTERVAL_S)
