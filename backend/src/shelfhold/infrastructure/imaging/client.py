"""The parent side: run the decode worker as a bounded subprocess and speak to it (ENF-SEC-003).

Nothing here imports Pillow. The worker gets no environment, an empty working directory, a new
session (so the whole group is killed on timeout), its own limits on memory and CPU, a wall-clock
deadline and a cap on what it may send back.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Final

import structlog

from shelfhold.infrastructure.imaging.caps import ImageCaps, ImageKind, caps_for
from shelfhold.infrastructure.imaging.errors import (
    DECODE_FAILED,
    WORKER_FAILED,
    ImageRejected,
)
from shelfhold.infrastructure.imaging.protocol import decode_frame, encode_frame

WORKER_MODULE: Final = "shelfhold.infrastructure.imaging.worker"
MAX_REPLY_BYTES: Final = 64 * 1024 * 1024
_BASE_MEMORY: Final = 512 * 1024 * 1024
_BYTES_PER_PIXEL: Final = 12  # decoded buffer, EXIF transpose copy, mode conversion
_THUMBNAIL_FORMATS: Final = frozenset({"webp", "jpeg", "png"})


@dataclass(frozen=True, slots=True)
class WorkerLimits:
    """What one worker run may use. `memory_bytes` is RLIMIT_AS, `cpu_seconds` RLIMIT_CPU."""

    memory_bytes: int = 2 * 1024**3
    cpu_seconds: int = 30
    wall_seconds: int = 60
    network_namespace: bool = True  # False forces the Python-level fallback (tests)

    @classmethod
    def for_caps(cls, caps: ImageCaps) -> WorkerLimits:
        return cls(memory_bytes=_BASE_MEMORY + _BYTES_PER_PIXEL * caps.max_pixels)


@dataclass(frozen=True, slots=True)
class ImageInfo:
    format: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Thumbnail:
    data: bytes
    format: str
    width: int
    height: int


def _argv(limits: WorkerLimits) -> list[str]:
    spec = json.dumps(
        {
            "memory_bytes": limits.memory_bytes,
            "cpu_seconds": limits.cpu_seconds,
            "netns": limits.network_namespace,
        }
    )
    return [sys.executable, "-I", "-m", WORKER_MODULE, spec]


_mode_logged = threading.Event()


def _log_sandbox_mode(mode: object) -> None:
    """Once per process: which isolation the worker really got (netns, seccomp or python)."""
    if mode is None or _mode_logged.is_set():
        return
    _mode_logged.set()
    log = structlog.get_logger()
    if mode == "python":
        log.warning("decode_worker_sandbox", mode=mode, network="python-level only")
    else:
        log.info("decode_worker_sandbox", mode=mode)


def _out_of_budget(reason: str) -> ImageRejected:
    """The worker used its time or CPU: the image is the cause, not the worker."""
    return ImageRejected(DECODE_FAILED, reason, "decode")


def _failed(reason: str) -> ImageRejected:
    return ImageRejected(WORKER_FAILED, reason, "worker")


def _collect(proc: subprocess.Popen[bytes], request: bytes, deadline: float) -> bytes:
    """Feed stdin from a thread, read stdout up to MAX_REPLY_BYTES before the deadline."""
    stdin, stdout = proc.stdin, proc.stdout
    if stdin is None or stdout is None:
        raise _failed("no pipes")

    def feed() -> None:
        with contextlib.suppress(OSError):
            stdin.write(request)
        with contextlib.suppress(OSError):
            stdin.close()

    threading.Thread(target=feed, daemon=True).start()
    fd = stdout.fileno()
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _out_of_budget("timeout")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise _out_of_budget("timeout")
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_REPLY_BYTES:
            raise _failed("reply too large")
        chunks.append(chunk)


def run_worker(
    op: str,
    args: dict[str, Any],
    data: bytes,
    *,
    limits: WorkerLimits | None = None,
    argv: list[str] | None = None,
) -> tuple[dict[str, Any], bytes]:
    """One request to a fresh worker. Returns the reply's result and payload, or raises
    ImageRejected (a refusal by the worker keeps its code; anything else is `image.worker_failed`).

    `argv` replaces the worker command: for tests of the parent's own timeout and parsing.
    """
    limits = limits or WorkerLimits()
    request = encode_frame({"op": op, "args": args}, data)
    deadline = time.monotonic() + limits.wall_seconds
    with tempfile.TemporaryDirectory(prefix="shelfhold-decode-") as workdir:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user-controlled word
            argv or _argv(limits),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=workdir,
            env={},
            close_fds=True,
            start_new_session=True,
        )
        started = time.monotonic()
        try:
            raw = _collect(proc, request, deadline)
        except ImageRejected:
            _kill(proc)
            raise
        finally:
            _kill(proc)
        rc = proc.returncode
        elapsed = time.monotonic() - started
    if not raw and (
        rc == -signal.SIGXCPU or (rc == -signal.SIGKILL and elapsed >= limits.cpu_seconds)
    ):
        raise _out_of_budget("cpu")
    try:
        header, payload = decode_frame(raw, MAX_REPLY_BYTES)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _failed("bad reply") from exc
    _log_sandbox_mode(header.get("sandbox"))
    if header.get("ok") is True and isinstance(header.get("result"), dict):
        return header["result"], payload
    code, reason, stage = header.get("code"), header.get("reason"), header.get("stage")
    if all(isinstance(v, str) for v in (code, reason, stage)) and str(code).startswith("image."):
        raise ImageRejected(str(code), str(reason), str(stage))
    raise _failed("bad reply")


def _kill(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL does not wait on anything
        proc.kill()


def _caps_args(caps: ImageCaps) -> dict[str, Any]:
    return {
        "max_pixels": caps.max_pixels,
        "max_bytes": caps.max_bytes,
        "max_height": caps.max_height,
        "formats": list(caps.formats),
    }


def _check_input(data: bytes, caps: ImageCaps) -> None:
    if len(data) > caps.max_bytes:
        raise ImageRejected("image.too_large", "bytes", "input")


def probe_image(
    data: bytes,
    kind: ImageKind | str,
    *,
    caps: ImageCaps | None = None,
    limits: WorkerLimits | None = None,
) -> ImageInfo:
    """Format and size of an untrusted image, fully decoded once inside the worker."""
    caps = caps or caps_for(kind)
    _check_input(data, caps)
    result, _ = run_worker(
        "probe", {"caps": _caps_args(caps)}, data, limits=limits or WorkerLimits.for_caps(caps)
    )
    return ImageInfo(str(result["format"]), int(result["width"]), int(result["height"]))


def make_thumbnail(
    data: bytes,
    kind: ImageKind | str,
    *,
    max_edge: int,
    fmt: str = "webp",
    caps: ImageCaps | None = None,
    limits: WorkerLimits | None = None,
) -> Thumbnail:
    """A re-encoded, EXIF-oriented, metadata-free thumbnail of an untrusted image."""
    if not 0 < max_edge <= 4096:
        raise ValueError("max_edge must be between 1 and 4096")
    if fmt not in _THUMBNAIL_FORMATS:
        raise ValueError(f"fmt must be one of {sorted(_THUMBNAIL_FORMATS)}")
    caps = caps or caps_for(kind)
    _check_input(data, caps)
    result, payload = run_worker(
        "thumbnail",
        {"caps": _caps_args(caps), "max_edge": max_edge, "fmt": fmt},
        data,
        limits=limits or WorkerLimits.for_caps(caps),
    )
    return Thumbnail(payload, str(result["format"]), int(result["width"]), int(result["height"]))


def diagnose_worker(
    what: str, *, limits: WorkerLimits | None = None, **args: Any
) -> dict[str, Any]:
    """Facts about the sandbox the worker really runs in: `network`, `limits`, `environment`."""
    result, _ = run_worker(what, args, b"", limits=limits)
    return result


__all__ = [
    "ImageInfo",
    "Thumbnail",
    "WorkerLimits",
    "diagnose_worker",
    "make_thumbnail",
    "probe_image",
    "run_worker",
]
