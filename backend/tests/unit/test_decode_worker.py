"""ENF-SEC-003: the decode worker is a bounded subprocess without network."""

from __future__ import annotations

import socket
import sys
import threading
import time

import pytest
from shelfhold.infrastructure.imaging import (
    ImageRejected,
    WorkerLimits,
    diagnose_worker,
    run_worker,
)


@pytest.fixture
def listener():
    """A loopback TCP server that records whether anybody ever connected to it."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    server.settimeout(0.5)
    hits: list[int] = []
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except (TimeoutError, OSError):
                continue
            hits.append(1)
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield server.getsockname()[1], hits
    stop.set()
    thread.join(2)
    server.close()


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_has_no_network(listener) -> None:
    port, hits = listener
    report = diagnose_worker("network", port=port)
    assert report["isolation"] in {"netns", "python"}
    # Python level, always on: no socket can be created through the standard library
    assert report["python_socket"] == "denied"
    # kernel level: when the worker got its own network namespace, not even a raw libc connect
    # reaches the loopback server (the namespace has no route and a down loopback). Without
    # namespace support the limit is documented (docs/security): a native exploit could connect.
    if report["isolation"] == "netns":
        assert report["raw_connect"] == "failed"
        time.sleep(0.6)
        assert hits == []
    else:
        assert report["raw_connect"] in {"failed", "connected", "unavailable"}


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_applies_its_limits_and_cannot_raise_them() -> None:
    limits = WorkerLimits(memory_bytes=3 * 1024**3, cpu_seconds=7, wall_seconds=20)
    report = diagnose_worker("limits", limits=limits)
    assert report["rlimit_as"] == [3 * 1024**3, 3 * 1024**3]  # soft and hard: not revocable
    assert report["rlimit_cpu"] == [7, 7]
    assert report["rlimit_core"] == [0, 0]
    assert report["no_new_privs"] is True


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_runs_in_an_isolated_clean_interpreter() -> None:
    report = diagnose_worker("environment")
    assert report["isolated"] is True  # python -I: no PYTHON* variables, no user site
    assert report["env_keys"] == []  # nothing inherited: no secret, no proxy, no token
    assert report["cwd_is_empty_dir"] is True
    assert report["pid"] != 0


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_wall_clock_timeout_kills_the_process() -> None:
    started = time.monotonic()
    with pytest.raises(ImageRejected) as caught:
        run_worker(
            "probe",
            {},
            b"",
            limits=WorkerLimits(wall_seconds=1),
            argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        )
    assert caught.value.code == "image.worker_failed"
    assert caught.value.reason == "timeout"
    assert time.monotonic() - started < 10


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_crash_or_garbage_reply_is_a_stable_error() -> None:
    for code in ("import os; os._exit(139)", "print('not a frame')", "pass"):
        with pytest.raises(ImageRejected) as caught:
            run_worker("probe", {}, b"", argv=[sys.executable, "-c", code])
        assert caught.value.code == "image.worker_failed"


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_memory_limit_turns_an_oversized_decode_into_an_error(malicious) -> None:
    from shelfhold.infrastructure.imaging import ImageKind, probe_image

    png = malicious.png_solid(6_000, 6_000)  # 36 Mpx is allowed by the caps ...
    with pytest.raises(ImageRejected) as caught:  # ... but not by a 160 MiB address space
        probe_image(png, ImageKind.PAGE, limits=WorkerLimits(memory_bytes=160 * 1024**2))
    assert caught.value.code in {"image.worker_failed", "image.decode_failed"}
