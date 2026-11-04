"""Smoke and privacy tests on the built image (ENF-SEC-006, ENF-PRIV-001, ENF-OPS-003)."""

import json
import time

from conftest import compose, exec_python, wait_for

READYZ = (
    "import urllib.request as u; r = u.urlopen('http://127.0.0.1:8080/readyz', timeout=5); "
    "print(r.status, r.read().decode())"
)
PROBE_EGRESS = (
    "import socket\n"
    "s = socket.socket(); s.settimeout(1)\n"
    "try: s.connect(('192.0.2.1', 80))\n"
    "except OSError: pass\n"
)


def egress_lines() -> list[str]:
    logs = compose("logs", "--no-color", "--no-log-prefix", "egress-watch").stdout
    return [line for line in logs.splitlines() if line.startswith("EGRESS")]


def test_stack_is_ready_after_up_wait(stack: None) -> None:
    status, body = exec_python("shelfhold", READYZ).split(" ", 1)
    assert status == "200"
    assert json.loads(body)["status"] == "ready"


def test_container_runs_as_uid_1000_with_read_only_root(stack: None) -> None:
    out = exec_python(
        "shelfhold",
        "import os; print(os.getuid()); "
        "\ntry:\n open('/app-write-probe','w')\nexcept OSError as e: print(e.errno)",
    ).split()
    assert out == ["1000", "30"]  # EROFS


def test_volumes_are_distinct_and_writable(stack: None) -> None:
    out = exec_python(
        "shelfhold",
        "import os; print(os.access('/data', os.W_OK), os.access('/store', os.W_OK), "
        "os.path.ismount('/data'), os.path.ismount('/store'))",
    )
    assert out.split() == ["True"] * 4


def test_no_outbound_request_on_fresh_install(stack: None) -> None:
    assert wait_for(lambda: "WATCH-READY" in compose("logs", "egress-watch").stdout)
    exec_python("shelfhold", READYZ)
    time.sleep(5)  # a fresh install idles: no update check, no telemetry (ENF-GOV-004)
    assert egress_lines() == []


def test_egress_watch_detects_a_deliberate_connection(stack: None) -> None:
    exec_python("shelfhold", PROBE_EGRESS)
    assert wait_for(lambda: any("192.0.2.1:80" in line for line in egress_lines()))
