"""Smoke and privacy tests on the built image (ENF-SEC-006, ENF-PRIV-001, ENF-OPS-003)."""

import json
import time

import pytest

from conftest import compose, exec_python, wait_for

READYZ = (
    "import urllib.request as u; r = u.urlopen('http://127.0.0.1:8080/readyz', timeout=5); "
    "print(r.status, r.read().decode())"
)
# Deliberate attempts of the positive controls; the fresh-install test ignores exactly these, so
# that the tests do not depend on their execution order.
CONTROL_TCP = "192.0.2.1:80"
CONTROL_HTTP = "192.0.2.2:8080"
CONTROL_DNS = "control-probe.example.org"
CONTROLS = (CONTROL_TCP, CONTROL_HTTP, CONTROL_DNS)
PROBE_TCP = (
    "import socket\n"
    "s = socket.socket(); s.settimeout(1)\n"
    "try: s.connect(('192.0.2.1', 80))\n"
    "except OSError: pass\n"
)
PROBE_HTTP = (
    "import urllib.request as u\n"
    "try: u.urlopen('http://192.0.2.2:8080/', timeout=1)\n"
    "except OSError: pass\n"
)
PROBE_DNS = (
    "import socket\n"
    "try: socket.getaddrinfo('control-probe.example.org', 80)\n"
    "except OSError: pass\n"
)


def egress_lines() -> list[str]:
    logs = compose("logs", "--no-color", "--no-log-prefix", "egress-watch").stdout
    return [line for line in logs.splitlines() if line.startswith("EGRESS")]


@pytest.mark.req("ENF-OPS-003")
def test_stack_is_ready_after_up_wait(stack: None) -> None:
    status, body = exec_python("shelfhold", READYZ).split(" ", 1)
    assert status == "200"
    assert json.loads(body)["status"] == "ready"


@pytest.mark.req("ENF-SEC-006")
def test_container_runs_as_uid_1000_with_read_only_root(stack: None) -> None:
    out = exec_python(
        "shelfhold",
        "import os; print(os.getuid()); "
        "\ntry:\n open('/app-write-probe','w')\nexcept OSError as e: print(e.errno)",
    ).split()
    assert out == ["1000", "30"]  # EROFS


@pytest.mark.req("ENF-OPS-012")
def test_volumes_are_distinct_and_writable(stack: None) -> None:
    out = exec_python(
        "shelfhold",
        "import os; print(os.access('/data', os.W_OK), os.access('/store', os.W_OK), "
        "os.path.ismount('/data'), os.path.ismount('/store'))",
    )
    assert out.split() == ["True"] * 4


@pytest.mark.req("ENF-PRIV-001")
def test_no_outbound_request_on_fresh_install(stack: None) -> None:
    assert wait_for(lambda: "WATCH-READY" in compose("logs", "egress-watch").stdout)
    exec_python("shelfhold", READYZ)
    time.sleep(5)  # a fresh install idles: no update check, no telemetry (ENF-GOV-004)
    unexpected = [line for line in egress_lines() if not any(c in line for c in CONTROLS)]
    assert unexpected == []


@pytest.mark.req("ENF-PRIV-001")
def test_egress_watch_detects_tcp_http_and_dns(stack: None) -> None:
    """Positive control: the watcher would notice a raw connection, an HTTP call and a DNS query."""
    for probe in (PROBE_TCP, PROBE_HTTP, PROBE_DNS):
        exec_python("shelfhold", probe)
    for marker in CONTROLS:
        assert wait_for(lambda m=marker: any(m in line for line in egress_lines())), marker


def test_root_serves_the_spa_index(stack: None) -> None:
    out = exec_python(
        "shelfhold",
        "import urllib.request as u; r = u.urlopen('http://127.0.0.1:8080/', timeout=5); "
        "print(r.status, r.headers.get_content_type()); print(r.read(2000).decode().lower())",
    )
    status, content_type = out.splitlines()[0].split()
    assert (status, content_type) == ("200", "text/html")
    assert "<html" in out
