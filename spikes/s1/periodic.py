"""Spike S1 criterion 3, accelerated: ~11 minutes instead of 24 h.

A: consumer up 5 min (drift of the per-minute crontab against the minute boundary).
B: consumer down 3.3 min (stand-in for the 2 h stop: 3 ticks are missed).
C: restart: the Shelfhold layer (last_run_at) enqueues ONE catch-up; consumer up 3 more minutes.

    uv run python periodic.py > periodic.json
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
workdir = Path(tempfile.mkdtemp(prefix="s1c3-"))
os.environ["S1_DIR"] = str(workdir)
sys.path.insert(0, str(HERE))
import layer  # noqa: E402
import tasks_periodic  # noqa: E402


def consumer() -> subprocess.Popen[bytes]:
    env = {**os.environ, "S1_DIR": str(workdir), "PYTHONPATH": str(HERE)}
    return subprocess.Popen(
        [sys.executable, "-m", "huey.bin.huey_consumer", "tasks_periodic.huey", "-w", "2", "-k", "thread"],
        cwd=HERE, env=env, stdout=open(workdir / "consumer.log", "ab"), stderr=subprocess.STDOUT,  # noqa: SIM115
    )


def runs() -> list[tuple[str, float]]:
    db = sqlite3.connect(workdir / "periodic.db", timeout=30)
    rows = db.execute("SELECT kind, at FROM runs ORDER BY at").fetchall()
    db.close()
    return rows


def wait_ticks(n: int, since: int, timeout: float) -> None:
    end = time.time() + timeout
    while time.time() < end and sum(1 for k, _ in runs() if k == "cron") < since + n:
        time.sleep(1)


t_start = time.time()
c = consumer()
wait_ticks(5, 0, 400)
phase_a_end = time.time()
c.terminate()
c.wait(20)
last_cron_before_stop = max(at for k, at in runs() if k == "cron")
time.sleep(200)  # 3.3 minutes down: at least 3 minute boundaries are missed
restart = time.time()
fired = layer.catch_up(workdir / "slots.db", {"tick": 60.0}, lambda _n: tasks_periodic.tick_catchup())
c = consumer()
before = sum(1 for k, _ in runs() if k == "cron")
wait_ticks(3, before, 300)
c.terminate()
c.wait(20)

rows = runs()
cron = [at for k, at in rows if k == "cron"]
drift = [at - (at // 60) * 60 for at in cron]
drift = [d if d < 30 else d - 60 for d in drift]  # signed offset to the nearest minute boundary
catchups = [at for k, at in rows if k == "catchup"]
cron_missed_window = [at for at in cron if last_cron_before_stop < at < restart]
cron_after_restart_first_minute = [at for at in cron if restart <= at < restart + 60]
print(json.dumps({
    "wall_seconds": round(time.time() - t_start),
    "cron_runs": len(cron),
    "drift_seconds": {"min": round(min(drift), 3), "max": round(max(drift), 3), "per_run": [round(d, 3) for d in drift]},
    "downtime_seconds": round(restart - phase_a_end),
    "missed_ticks_executed_by_huey_during_downtime": len(cron_missed_window),
    "catch_up_fired_by_layer": fired,
    "catch_up_runs": len(catchups),
    "cron_runs_in_first_60s_after_restart": len(cron_after_restart_first_minute),
    "pass_drift_within_2s": max(abs(d) for d in drift) <= 2.0,
    "pass_exactly_one_catch_up": len(catchups) == 1,
}, indent=2))
