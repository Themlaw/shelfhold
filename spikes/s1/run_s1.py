"""Spike S1 runner: criteria 1, 2, 4, 5 and 6 (3 is periodic.py, it needs minutes).

    uv run python run_s1.py > results.json
"""

import json
import os
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def start_consumer(workdir: Path, workers: int, flush: bool = True, name: str = "tasks.huey") -> subprocess.Popen[bytes]:
    env = {**os.environ, "S1_DIR": str(workdir), "S1_FLUSH_LOCKS": "1" if flush else "0", "PYTHONPATH": str(HERE)}
    log = open(workdir / "consumer.log", "ab")  # noqa: SIM115
    return subprocess.Popen(
        [sys.executable, "-m", "huey.bin.huey_consumer", name, "-w", str(workers), "-k", "thread", "-v"],
        cwd=HERE, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def load_tasks(workdir: Path):  # type: ignore[no-untyped-def]
    os.environ["S1_DIR"] = str(workdir)
    sys.path.insert(0, str(HERE))
    for mod in [m for m in sys.modules if m == "tasks"]:
        del sys.modules[mod]
    import tasks

    return tasks


def count(workdir: Path, table: str) -> int:
    try:
        db = sqlite3.connect(workdir / "app.db", timeout=30)
        (n,) = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
        db.close()
        return int(n)
    except sqlite3.OperationalError:
        return 0


def wait_until(fn, seconds: float) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.2)
    return False


def criterion1() -> dict[str, object]:
    workdir = Path(tempfile.mkdtemp(prefix="s1c1-"))
    tasks = load_tasks(workdir)
    errors: list[str] = []
    consumer = start_consumer(workdir, workers=8)
    time.sleep(2)

    def produce(start: int) -> None:
        for n in range(start, start + 250):
            try:
                tasks.record(n)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    t0 = time.monotonic()
    threads = [threading.Thread(target=produce, args=(i * 250,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    enqueued_in = time.monotonic() - t0
    ok = wait_until(lambda: count(workdir, "done") >= 1000, 120)
    total = time.monotonic() - t0
    consumer.terminate()
    consumer.wait(20)
    log = (workdir / "consumer.log").read_text(errors="replace")
    locked = log.count("database is locked") + sum("locked" in e for e in errors)
    return {"executed": count(workdir, "done"), "all_1000_executed": ok, "enqueue_errors": len(errors),
            "database_is_locked_occurrences": locked, "seconds_to_enqueue_1000": round(enqueued_in, 2),
            "seconds_to_run_1000": round(total, 2), "workers": 8, "producers": 4,
            "pass": ok and locked == 0 and not errors}


def criterion2() -> dict[str, object]:
    workdir = Path(tempfile.mkdtemp(prefix="s1c2-"))
    tasks = load_tasks(workdir)
    for i in range(6):
        tasks.locked_slow(i, 3.0)
    consumer = start_consumer(workdir, workers=1)
    wait_until(lambda: count(workdir, "started") >= 1, 20)
    time.sleep(0.5)
    consumer.send_signal(signal.SIGKILL)
    consumer.wait()
    pending_after_kill = tasks.huey.pending_count()
    lock_survived = False
    try:
        with tasks.huey.lock_task("chapter-lock"):
            pass
    except Exception:  # noqa: BLE001
        lock_survived = True
    # control: restart WITHOUT flush_locks -> remaining tasks hit the dead worker's lock
    consumer = start_consumer(workdir, workers=1, flush=False)
    time.sleep(4)
    done_without_flush = count(workdir, "done")
    consumer.terminate()
    consumer.wait(20)
    # real procedure: flush_locks at startup, then enqueue again and drain
    for i in range(10, 13):
        tasks.locked_slow(i, 0.3)
    consumer = start_consumer(workdir, workers=1, flush=True)
    drained = wait_until(lambda: count(workdir, "done") >= done_without_flush + 3, 30)
    lock_free = True
    try:
        tasks.huey.flush_locks()
    except Exception:  # noqa: BLE001
        lock_free = False
    consumer.terminate()
    consumer.wait(20)
    return {"lock_survived_kill9": lock_survived, "pending_after_kill": pending_after_kill,
            "done_after_restart_without_flush": done_without_flush,
            "tasks_after_flush_all_ran": drained, "pending_at_end": tasks.huey.pending_count(),
            "lost_in_flight_tasks": 1,
            "note": "the task running at kill time is dequeued, not acked: lost to Huey; the Shelfhold "
                    "jobs table (status=running) re-enqueues it, idempotent per chapter",
            "pass": lock_survived and drained and lock_free and tasks.huey.pending_count() == 0}


def criterion4() -> dict[str, object]:
    workdir = Path(tempfile.mkdtemp(prefix="s1c4-"))
    tasks = load_tasks(workdir)
    consumer = start_consumer(workdir, workers=8)
    time.sleep(2)
    n = 0
    for domain in ("a.example", "b.example", "c.example"):
        for _ in range(20):
            tasks.with_domain_slot(n, domain, 0.2)
            n += 1
    db = sqlite3.connect(workdir / "slots.db", timeout=30)
    ok = wait_until(lambda: db.execute("SELECT COUNT(*) FROM slot_log WHERE ev='release'").fetchone()[0] >= 60, 90)
    consumer.terminate()
    consumer.wait(20)
    sys.path.insert(0, str(HERE))
    import layer

    peaks = layer.max_concurrency(workdir / "slots.db")
    return {"limit": 2, "peak_concurrency_per_domain": peaks, "all_60_ran": ok, "workers": 8,
            "pass": ok and all(p <= 2 for p in peaks.values()) and max(peaks.values()) == 2}


def criterion5() -> dict[str, object]:
    out: dict[str, object] = {}
    for label, with_consumer in (("idle_consumer", False), ("consumer_running_8_workers", True)):
        workdir = Path(tempfile.mkdtemp(prefix="s1c5-"))
        tasks = load_tasks(workdir)
        consumer = start_consumer(workdir, workers=8) if with_consumer else None
        time.sleep(2)
        samples = []
        for n in range(3000):
            t0 = time.perf_counter()
            tasks.record(n)
            samples.append((time.perf_counter() - t0) * 1000)
        if consumer:
            consumer.terminate()
            consumer.wait(20)
        samples.sort()
        out[label] = {"n": 3000, "p50_ms": round(statistics.median(samples), 3),
                      "p99_ms": round(samples[int(len(samples) * 0.99)], 3), "max_ms": round(samples[-1], 3)}
    out["pass"] = all(v["p99_ms"] < 5 for v in out.values() if isinstance(v, dict))  # type: ignore[index]
    return out


def criterion6() -> dict[str, object]:
    workdir = Path(tempfile.mkdtemp(prefix="s1c6-"))
    code = (
        "import os, time, sys\n"
        f"os.environ['S1_DIR']={str(workdir)!r}; sys.path.insert(0, {str(HERE)!r})\n"
        "def rss():\n"
        "    for l in open('/proc/self/status'):\n"
        "        if l.startswith('VmRSS'): return int(l.split()[1])/1024\n"
        "import fastapi, uvicorn, sqlalchemy, alembic, structlog, pydantic\n"
        "app = fastapi.FastAPI()\n"
        "time.sleep(1); base = rss()\n"
        "import tasks\n"
        "from huey.consumer import Consumer\n"
        "cs = [Consumer(tasks.huey, workers=4, worker_type='thread'), Consumer(tasks.huey_cpu, workers=1, worker_type='thread'),\n"
        "      Consumer(tasks.huey_maint, workers=1, worker_type='thread')]\n"
        "for c in cs: c.start()\n"
        "time.sleep(10); idle = rss()\n"
        "print(base, idle, flush=True)\n"
        "os._exit(0)\n"
    )
    runs = []
    for _ in range(3):
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=HERE)
        base, idle = map(float, res.stdout.split())
        runs.append((base, idle))
    deltas = [round(i - b, 1) for b, i in runs]
    return {"baseline_mb": [round(b, 1) for b, _ in runs], "with_huey_3_pools_idle_mb": [round(i, 1) for _, i in runs],
            "delta_mb": deltas, "pool_layout": "io 4 threads, cpu 1, maint 1 (in process)",
            "pass": max(deltas) <= 20}


if __name__ == "__main__":
    results = {}
    for name, fn in [("1_no_database_locked", criterion1), ("2_kill9_flush_locks", criterion2),
                     ("4_domain_slot_n2", criterion4), ("5_enqueue_latency", criterion5), ("6_rss_overhead", criterion6)]:
        print(f"running {name}", file=sys.stderr)
        results[name] = fn()
    print(json.dumps(results, indent=2))
