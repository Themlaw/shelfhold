"""Plugin of tests/conftest.py: a test that starts the application's threads (Huey consumers and
their workers, the health reads, the roots look...) and does not stop them fails, by name.

A thread that outlives its test writes into the output of the next one (a `job.started` line in
the JSON a command prints), holds a connection to a database the next test deletes (`database is
locked`) and keeps the process busy. The check is per test, not per session: it is the only way to
name the test that leaked. A thread already alive before the test is not its doing.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator

import pytest

# The application's own threads: by name (Huey's, ours) or by the module of their target.
_NAMES = re.compile(r"^(Worker-|Scheduler|Cron|huey|health-|shelfhold)", re.IGNORECASE)
_MODULES = ("shelfhold", "huey")
_GRACE_S = 5.0


def _ours(thread: threading.Thread) -> bool:
    if _NAMES.match(thread.name):
        return True
    target = getattr(thread, "_target", None)
    module = getattr(target, "__module__", "") or ""
    return module.split(".")[0] in _MODULES


def _alive() -> dict[int, threading.Thread]:
    return {t.ident or id(t): t for t in threading.enumerate() if t.is_alive()}


@pytest.fixture(autouse=True)
def _no_application_thread_outlives_the_test() -> Iterator[None]:
    before = set(_alive())
    yield
    deadline = time.monotonic() + _GRACE_S
    while True:
        left = [t for ident, t in _alive().items() if ident not in before and _ours(t)]
        if not left or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if left:
        names = ", ".join(f"{t.name}{' (daemon)' if t.daemon else ''}" for t in left)
        pytest.fail(f"threads of the application outlive the test: {names}", pytrace=False)
