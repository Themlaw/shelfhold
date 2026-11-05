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
    assert report["isolation"] in {"netns", "seccomp", "python"}
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
    assert report["rlimit_cpu"] == [7, 9]  # SIGXCPU at 7 s (caught), SIGKILL two seconds later
    assert report["rlimit_nproc"] == [1, 1]  # no thread, no process
    assert report["rlimit_core"] == [0, 0]
    assert report["no_new_privs"] is True


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_runs_in_an_isolated_clean_interpreter() -> None:
    report = diagnose_worker("environment")
    assert report["isolated"] is True  # python -I: no PYTHON* variables, no user site
    assert set(report["env_keys"]) <= {
        "LC_CTYPE"
    }  # nothing inherited (CPython sets LC_CTYPE itself)
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
    # an image that keeps the worker busy past the deadline is the image's fault (review of #40)
    assert caught.value.code == "image.decode_failed"
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

    png = malicious.png_solid(8_000, 8_000)  # 64 Mpx is allowed by the caps ...
    with pytest.raises(ImageRejected) as caught:  # ... but not by a 96 MiB address space
        probe_image(png, ImageKind.PAGE, limits=WorkerLimits(memory_bytes=96 * 1024**2))
    assert caught.value.code in {"image.worker_failed", "image.decode_failed"}


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_python_level_fallback_when_no_namespace(listener) -> None:
    port, hits = listener
    report = diagnose_worker("network", port=port, limits=WorkerLimits(network_namespace=False))
    assert report["isolation"] in {"seccomp", "python"}
    assert report["python_socket"] == "denied"  # the stub is in place with or without a namespace
    if report["isolation"] == "seccomp":  # the real fallback of a container: no namespace allowed
        assert report["raw_connect"] == "failed"  # libc socket(AF_INET) answers EPERM
        time.sleep(0.6)
        assert hits == []


@pytest.mark.req("ENF-SEC-003")
@pytest.mark.parametrize("namespace", [True, False])
def test_decode_worker_cannot_start_a_process(namespace: bool) -> None:
    report = diagnose_worker("fork", limits=WorkerLimits(network_namespace=namespace))
    assert report["fork"] == "denied"  # seccomp refuses clone without CLONE_THREAD; NPROC too


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_seccomp_filter_is_built_per_architecture() -> None:
    from shelfhold.infrastructure.imaging.worker import seccomp_program

    for machine in ("x86_64", "aarch64"):
        program = seccomp_program(machine)
        assert program is not None
        assert len(program) % 8 == 0
        assert len(program) // 8 < 64
    assert seccomp_program("riscv64") is None  # unknown architecture: python mode, no filter


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_avif_still_decodes_without_extra_threads() -> None:
    import io

    from PIL import Image, features

    from shelfhold.infrastructure.imaging import ImageKind, probe_image

    if not features.check("avif"):
        pytest.skip("this Pillow has no AVIF")
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(out, "AVIF")
    info = probe_image(out.getvalue(), ImageKind.PAGE)
    assert (info.format, info.width, info.height) == ("AVIF", 64, 48)


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_cpu_exhaustion_is_a_decode_failure() -> None:
    code = "import resource\nresource.setrlimit(resource.RLIMIT_CPU, (1, 3))\nwhile 1: pass"
    with pytest.raises(ImageRejected) as caught:
        run_worker("probe", {}, b"", argv=[sys.executable, "-c", code])
    assert (caught.value.code, caught.value.reason) == ("image.decode_failed", "cpu")


@pytest.mark.req("ENF-SEC-003")
def test_decode_worker_logs_the_effective_sandbox_mode_once() -> None:
    import structlog.testing

    from shelfhold.infrastructure.imaging import client

    client._mode_logged.clear()
    with structlog.testing.capture_logs() as logs:
        diagnose_worker("limits")
        diagnose_worker("limits")
    events = [e for e in logs if e["event"] == "decode_worker_sandbox"]
    assert len(events) == 1
    assert events[0]["mode"] in {"netns", "seccomp", "python"}
