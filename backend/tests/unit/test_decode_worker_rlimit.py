"""ENF-PERF-009 (culpable): `RLIMIT_AS` is set on the decode worker before it reads one byte of an
image, and a giant decompressed image is refused cleanly, the process that asked living on.

The proof is read from the kernel: `/proc/<pid>/limits` of a live worker that has not been sent its
request yet (so no decoder can have run), and `getrlimit` inside the worker. Synthetic images only.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from types import ModuleType

import pytest

from shelfhold.infrastructure.imaging import (
    ImageKind,
    ImageRejected,
    WorkerLimits,
    caps_for,
    diagnose_worker,
    probe_image,
)
from shelfhold.infrastructure.imaging.client import _argv
from tests.bench.rss import MIB, process_rss

MEMORY_LINE = re.compile(r"^Max address space\s+(\S+)\s+(\S+)\s+bytes", re.MULTILINE)


def address_space_limit(pid: int) -> tuple[str, str] | None:
    try:
        text = Path(f"/proc/{pid}/limits").read_text()
    except OSError:
        return None
    found = MEMORY_LINE.search(text)
    return (found[1], found[2]) if found else None


@pytest.mark.req("ENF-PERF-009")
def test_decode_worker_rlimit_as_set() -> None:
    limits = WorkerLimits.for_caps(caps_for(ImageKind.PAGE))
    wanted = str(limits.memory_bytes)
    # a live worker, started and given no request: it has installed its sandbox and waits on
    # stdin, so whatever the kernel reports was set before any decoding could happen
    worker = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        _argv(limits),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={},
    )
    try:
        deadline = time.monotonic() + 20
        seen = address_space_limit(worker.pid)
        while seen != (wanted, wanted) and time.monotonic() < deadline:
            time.sleep(0.05)
            seen = address_space_limit(worker.pid)
        assert worker.poll() is None  # still waiting for its request
        assert seen == (wanted, wanted)  # soft and hard: the worker cannot raise it again
    finally:
        worker.kill()
        worker.wait()
    inside = diagnose_worker("limits", limits=limits)  # and getrlimit, from inside the worker
    assert inside["rlimit_as"] == [limits.memory_bytes, limits.memory_bytes]


@pytest.mark.req("ENF-PERF-009")
def test_giant_decompressed_image_is_refused_and_the_caller_lives_on(malicious: ModuleType) -> None:
    before = process_rss(os.getpid())
    # 64 Mpx is within the cap of a page, but not within a 96 MiB address space: the decoder
    # runs out of memory inside the worker, which answers, instead of taking anything down
    giant = malicious.png_solid(8_000, 8_000)
    with pytest.raises(ImageRejected) as caught:
        probe_image(giant, ImageKind.PAGE, limits=WorkerLimits(memory_bytes=96 * MIB))
    assert caught.value.code in {"image.decode_failed", "image.worker_failed"}
    # beyond the pixel cap, the header alone is enough to refuse it
    with pytest.raises(ImageRejected) as too_big:
        probe_image(malicious.png_header_only(100_000, 100_000), ImageKind.PAGE)
    assert (too_big.value.code, too_big.value.reason) == ("image.too_large", "pixels")
    # the caller never decoded anything itself and is still serving: a normal image goes through
    info = probe_image(malicious.png_solid(64, 64), ImageKind.PAGE)
    assert (info.format, info.width, info.height) == ("PNG", 64, 64)
    assert process_rss(os.getpid()) - before < 150 * MIB
