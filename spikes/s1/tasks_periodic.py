"""Criterion 3: a per-minute periodic task (accelerated stand-in for the real hourly/daily ones)."""

import os
import sqlite3
import time
from pathlib import Path

from huey import SqliteHuey, crontab

import layer

WORKDIR = Path(os.environ.get("S1_DIR", "."))
huey = SqliteHuey("periodic", filename=str(WORKDIR / "huey-periodic.db"))


def record(kind: str) -> None:
    db = sqlite3.connect(WORKDIR / "periodic.db", timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS runs (kind TEXT, at REAL)")
    db.execute("INSERT INTO runs VALUES (?, ?)", (kind, time.time()))
    db.commit()
    db.close()
    layer.mark_run(WORKDIR / "slots.db", "tick")


@huey.periodic_task(crontab(minute="*"))
def tick() -> None:
    record("cron")


@huey.task()
def tick_catchup() -> None:
    record("catchup")
