"""#341 (M4 L36), relecture adverse: each test is a hole found in the adoption of a foreign
library, red today, kept as a strict xfail until the fix (docs/decisions/M4.md, "#341 :
contradicteur").
"""

# ruff: noqa: F811  (the `world` fixture of L39 is imported, then asked for by name)

from __future__ import annotations

import contextlib
import copy
import errno
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tracemalloc
import zipfile
from pathlib import Path
from typing import Any

import pytest

from shelfhold.export.targets.kavita import adopt as adopt_module
from shelfhold.infrastructure.imaging import ImageRejected
from shelfhold.infrastructure.imaging import client as imaging_client
from shelfhold.infrastructure.imaging.protocol import encode_frame
from tests.adopt_env import adoption
from tests.export_env import export_env
from tests.integration.test_adopt_foreign_l36 import _flagged, _ops, _png
from tests.integration.test_l39_wizard import World, _existing_series, world  # noqa: F401
from tests.kavita_env import run
from tests.library_env import cbz_bytes

PREVIEW = "/api/v1/setup/adoption/preview"
CONFIRM = "/api/v1/setup/adoption/confirm"
PERF_PEAK_BYTES = 500_000_000  # ENF-PERF-001: 500 MB at peak for the whole process


def _stored_cbz(page: bytes) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("001.png", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, page)
    return buffer.getvalue()


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_screen_confirm_does_not_probe_the_library_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision says the adoption that follows a preview does not probe again. On the screen,
    `WizardOps._adopter` builds a new `Adopter` per request: its verdict cache is empty at the
    confirmation, and a 10 000-file library is probed twice (twice ~10 minutes, each inside one
    HTTP request)."""
    _existing_series(world)
    ops = _ops(monkeypatch)
    seen = world.post(PREVIEW)
    assert seen.status_code == 200, seen.text
    done = world.post(CONFIRM, {"fingerprint": seen.json()["fingerprint"]})
    assert done.status_code == 200, done.text
    assert ops == ["probe"]


@pytest.mark.req("EF-EXP-045", "ENF-PERF-001")
def test_core_page_buffers_stay_within_the_peak_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Four first pages of 60 MiB (under PROBE_MAX_BYTES): the core holds the batch (`read`) and,
    in each probing thread, the request frame `run_worker` builds (a second copy). The worker is
    replaced by a stand-in that builds the same frame and waits for the four others, as four real
    workers of ~0.2 s do."""
    barrier = threading.Barrier(adopt_module.PROBE_CONCURRENCY, timeout=20)

    def worker(op: str, args: dict[str, Any], data: bytes, **_: Any) -> Any:
        frame = encode_frame({"op": op, "args": args}, data)
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        del frame
        raise ImageRejected("image.decode_failed", "stand-in", "decode")

    monkeypatch.setattr(imaging_client, "run_worker", worker)
    page = b"\x89PNG\r\n\x1a\n" + b"\x00" * (60 * 1024 * 1024)
    archive = _stored_cbz(page)
    del page
    with export_env() as env:
        a = adoption(env)
        a.add_series("Big", {f"Big - Ch.{n}.cbz": archive for n in range(1, 5)})
        del archive
        tracemalloc.start()
        try:
            run(a.adopter.preview(a.wired.client, env.destination_id))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert peak < PERF_PEAK_BYTES, f"{peak / 1e6:.0f} MB of buffers in the core"


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_two_previews_share_one_bound_on_probe_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two previews at once (two tabs, a double click, the CLI and the screen): each opens its own
    pool of PROBE_CONCURRENCY workers, each worker allowed ~1.5 GiB (RLIMIT_AS of `page_input`).
    The thumbnails have a process-wide semaphore; the probes have none."""
    lock = threading.Lock()
    running = [0, 0]  # now, most

    def worker(op: str, args: dict[str, Any], data: bytes, **_: Any) -> Any:
        with lock:
            running[0] += 1
            running[1] = max(running)
        time.sleep(0.4)
        with lock:
            running[0] -= 1
        return {"format": "PNG", "width": 1, "height": 1}, b""

    with export_env() as env:
        a = adoption(env)
        a.add_series(
            "S", {f"S - Ch.{n}.cbz": cbz_bytes((("001.png", _png()),)) for n in range(1, 5)}
        )
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
        files = [f for s in report.series for f in s.files]
        monkeypatch.setattr(imaging_client, "run_worker", worker)
        adopters = []
        for _ in range(2):
            fresh = copy.copy(a.adopter)
            fresh._pages = {}  # a new request: a new Adopter
            adopters.append(fresh)
        threads = [threading.Thread(target=x._with_pages, args=(list(files),)) for x in adopters]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert running[1] <= adopt_module.PROBE_CONCURRENCY, running[1]


@pytest.mark.xfail(strict=True, reason="#341 m1: the verdict cache trusts a forged mtime")
@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_a_file_rewritten_in_place_with_its_mtime_put_back_is_probed_again() -> None:
    """Preview, then the page is rewritten in place (same size, same inode) and `utime` puts the
    mtime back: path, size, mtime and inode are those of the preview, so the cached "clean"
    verdict is reused and the fingerprint does not move. `st_ctime_ns` moved, and nobody looks."""
    clean = cbz_bytes((("001.png", _png()),))
    with export_env() as env:
        a = adoption(env)
        a.add_series("S", {"S - Ch.1.cbz": clean})
        seen = run(a.adopter.preview(a.wired.client, env.destination_id))
        assert _flagged(seen) == {}
        (file,) = (f for s in seen.series for f in s.files)
        before = os.stat(file.local_path)
        start = clean.index(b"\x89PNG")
        with open(file.local_path, "r+b") as stream:  # same inode, same size
            stream.seek(start)
            stream.write(b"X" * 16)  # no PNG any more (and the CRC is wrong)
        os.utime(file.local_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        done = run(a.adopter.adopt(a.wired.client, env.destination_id, confirm=seen.fingerprint))
    assert _flagged(done) != {}


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_a_worker_that_cannot_start_marks_the_file_not_the_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fork` refused (EAGAIN under load, ENOMEM): `Popen` raises OSError, which `_probe_page`
    does not catch: the whole preview fails instead of marking the file `image.worker_failed`."""

    def refuse(*_a: object, **_k: object) -> Any:
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    with export_env() as env:
        a = adoption(env)
        a.add_series("S", {"S - Ch.1.cbz": cbz_bytes((("001.png", _png()),))})
        monkeypatch.setattr(subprocess, "Popen", refuse)  # what the client calls
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
    assert _flagged(report) == {"S - Ch.1.cbz": "image.worker_failed"}


_UNIX_CONNECT = r"""
import ctypes, json, sys
from shelfhold.infrastructure.imaging import worker
state = worker._sandbox(json.loads(sys.argv[1]))
libc = ctypes.CDLL(None, use_errno=True)
fd = libc.socket(1, 1, 0)  # AF_UNIX, SOCK_STREAM
path = sys.argv[2].encode()
address = ctypes.create_string_buffer(b"\x01\x00" + path, 2 + len(path) + 1)
ok = fd >= 0 and libc.connect(fd, address, len(address)) == 0
print(json.dumps({"isolation": state["isolation"], "unix_connect": ok}))
"""


@pytest.mark.skipif(sys.platform != "linux", reason="the sandbox is Linux only")
@pytest.mark.req("ENF-SEC-003")
def test_a_shut_network_also_shuts_unix_sockets(tmp_path: Path) -> None:
    """The diagnostic bundle says `network: shut` for `netns` or `seccomp`. Neither stops a
    connect() to a Unix socket by path: the filter blocks AF_INET, AF_INET6 and AF_PACKET only,
    and a network namespace does not scope path sockets. A decoder flaw reaches any socket the
    process can see (a daemon's control socket, a mounted docker.sock)."""
    path = tmp_path / "s"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    try:
        spec = '{"memory_bytes": 1073741824, "cpu_seconds": 10, "netns": true}'
        done = subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, "-I", "-c", _UNIX_CONNECT, spec, str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            env={},
            check=True,
        )
    finally:
        server.close()
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["isolation"] in {"netns", "seccomp"}, result
    assert result["unix_connect"] is False, result
