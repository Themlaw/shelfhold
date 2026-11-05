"""The worker's pure parts and the parent's edge cases, run in this process (no sandbox).

The sandbox itself (unshare, seccomp, rlimits, main) is exercised by test_decode_worker.py in a
real subprocess, which coverage does not follow; everything else is tested here for coverage and
speed.
"""

from __future__ import annotations

import io
import signal
import struct
import sys
from types import ModuleType
from typing import Any

import pytest
from PIL import Image

from shelfhold.infrastructure.imaging import (
    ImageCaps,
    ImageKind,
    ImageRejected,
    WorkerLimits,
    caps_for,
    client,
    protocol,
    run_worker,
    worker,
)

pytestmark = pytest.mark.req("ENF-SEC-003")

CAPS: dict[str, Any] = {
    "max_pixels": 1_000_000,
    "max_bytes": 10_000_000,
    "max_height": None,
    "formats": ["JPEG", "PNG", "WEBP", "GIF"],
}


def _png(size: tuple[int, int] = (40, 20), mode: str = "RGB", **save: Any) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size).save(out, "PNG", **save)
    return out.getvalue()


def _reply(op: str, args: dict[str, Any], data: bytes) -> tuple[dict[str, Any], bytes]:
    request = protocol.encode_frame({"op": op, "args": args}, data)
    reply = worker.reply_for(io.BytesIO(request), {"isolation": "test"})
    return protocol.decode_frame(reply, 1 << 28)


# --- protocol -------------------------------------------------------------------------------


def test_protocol_frame_round_trip() -> None:
    blob = protocol.encode_frame({"a": 1}, b"xyz")
    assert protocol.decode_frame(blob, 10) == ({"a": 1}, b"xyz")
    assert protocol.read_frame(io.BytesIO(blob), 10) == ({"a": 1}, b"xyz")


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"\x00\x00",
        struct.pack(">II", protocol.MAX_HEADER_BYTES + 1, 0),
        struct.pack(">II", 2, 99) + b"{}",  # payload larger than allowed
        struct.pack(">II", 2, 1) + b"{}",  # length mismatch
        struct.pack(">II", 4, 0) + b"[1] ",  # not an object
        struct.pack(">II", 3, 0) + b"{x}",  # not JSON
    ],
)
def test_protocol_refuses_malformed_frames(blob: bytes) -> None:
    with pytest.raises(ValueError):
        protocol.decode_frame(blob, 50)
    with pytest.raises(ValueError):
        protocol.read_frame(io.BytesIO(blob), 50)


# --- worker: header checks, caps, formats ------------------------------------------------------


def test_probe_reports_format_and_size_and_keeps_pillow_globals() -> None:
    before = (Image.MAX_IMAGE_PIXELS, list(__import__("warnings").filters))
    header, _ = _reply("probe", {"caps": CAPS}, _png())
    assert header["result"] == {"format": "PNG", "width": 40, "height": 20}
    assert header["sandbox"] == "test"
    assert (Image.MAX_IMAGE_PIXELS, list(__import__("warnings").filters)) == before


@pytest.mark.parametrize(
    ("caps", "data", "expected"),
    [
        ({"max_bytes": 10}, _png(), ("image.too_large", "bytes", "input")),
        ({"max_pixels": 799}, _png(), ("image.too_large", "pixels", "header")),
        ({"max_pixels": 100}, _png(), ("image.too_large", "pixels", "header")),  # beyond 2x
        ({"max_height": 19}, _png(), ("image.too_large", "height", "header")),
        ({}, b"not an image", ("image.format_refused", "not an allowlisted image", "header")),
        (
            {"formats": ["JPEG"]},
            _png(),
            ("image.format_refused", "not an allowlisted image", "header"),
        ),
        ({}, _png()[:60], ("image.decode_failed", None, "decode")),
    ],
)
def test_probe_refusals(caps: dict[str, Any], data: bytes, expected: tuple[str, Any, str]) -> None:
    header, _ = _reply("probe", {"caps": {**CAPS, **caps}}, data)
    assert header["ok"] is False
    assert (header["code"], header["stage"]) == (expected[0], expected[2])
    if expected[1]:
        assert header["reason"] == expected[1]


def test_probe_bomb_warning_between_the_cap_and_twice_the_cap() -> None:
    header, _ = _reply("probe", {"caps": {**CAPS, "max_pixels": 700}}, _png())  # 800 px: warning
    assert (header["code"], header["reason"]) == ("image.too_large", "pixels")


def test_guarded_maps_decoder_failures_and_bombs() -> None:
    def boom(exc: BaseException) -> Any:
        def call() -> None:
            raise exc

        return call

    for exc, code in [
        (Image.DecompressionBombError("x"), "image.too_large"),
        (MemoryError(), "image.decode_failed"),
        (OSError("x"), "image.decode_failed"),
        (ZeroDivisionError(), "image.decode_failed"),
    ]:
        with pytest.raises(worker._Reject) as caught:
            worker._guarded(boom(exc))
        assert caught.value.code == code


def test_reply_for_turns_memory_error_and_bad_requests_into_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def oom(*_: Any) -> Any:
        raise MemoryError

    monkeypatch.setattr(worker, "_handle", oom)
    header, _ = _reply("probe", {}, b"")
    assert (header["code"], header["reason"]) == ("image.decode_failed", "memory")


def test_unknown_operation_is_a_worker_failure() -> None:
    header, _ = _reply("rm-rf", {}, b"")
    assert (header["code"], header["stage"]) == ("image.worker_failed", "worker")


# --- worker: thumbnails ------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["webp", "jpeg", "png"])
@pytest.mark.parametrize("mode", ["RGB", "RGBA", "P", "L", "CMYK", "I;16"])
def test_thumbnail_every_mode_and_format(fmt: str, mode: str) -> None:
    source = _png((60, 30), mode) if mode != "CMYK" else _tiff_free_cmyk()
    header, payload = _reply("thumbnail", {"caps": CAPS, "max_edge": 20, "fmt": fmt}, source)
    assert header["ok"] is True, header
    assert max(header["result"]["width"], header["result"]["height"]) == 20
    assert Image.open(io.BytesIO(payload)).format == fmt.upper()


def _tiff_free_cmyk() -> bytes:
    out = io.BytesIO()
    Image.new("CMYK", (60, 30)).save(out, "JPEG")
    return out.getvalue()


def test_thumbnail_keeps_transparency_of_a_palette_png() -> None:
    palette = Image.new("P", (10, 10), 0)
    palette.info["transparency"] = 0
    out = io.BytesIO()
    palette.save(out, "PNG", transparency=0)
    header, payload = _reply(
        "thumbnail", {"caps": CAPS, "max_edge": 8, "fmt": "png"}, out.getvalue()
    )
    assert Image.open(io.BytesIO(payload)).mode == "RGBA"
    assert header["result"]["format"] == "PNG"


def test_thumbnail_of_a_jpeg_uses_draft_mode() -> None:
    out = io.BytesIO()
    Image.new("RGB", (400, 400)).save(out, "JPEG")
    header, _ = _reply("thumbnail", {"caps": CAPS, "max_edge": 20, "fmt": "webp"}, out.getvalue())
    assert header["result"]["width"] == 20


# --- worker: diagnostics, limits handler -------------------------------------------------------


def test_diagnostics_that_are_safe_in_process() -> None:
    state = {"isolation": "x", "seccomp": False}
    assert worker._diagnose("limits", {}, state)["rlimit_as"]
    env = worker._diagnose("environment", {}, state)
    assert env["pid"] > 0
    assert worker._diagnose("network", {"port": 9}, state)["raw_connect"] in {
        "failed",
        "unavailable",
    }
    assert worker._python_socket() == "allowed"  # this process is not sandboxed
    with pytest.raises(worker._Reject):
        worker._diagnose("nope", {}, state)


def test_cpu_limit_handler_writes_a_decode_failed_frame_and_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: list[tuple[int, bytes]] = []
    monkeypatch.setattr(worker.os, "write", lambda fd, data: written.append((fd, data)))

    def stop(code: int) -> None:
        raise SystemExit(code)

    monkeypatch.setattr(worker.os, "_exit", stop)
    with pytest.raises(SystemExit):
        worker._on_cpu_limit(signal.SIGXCPU, None)
    header, _ = protocol.decode_frame(written[0][1], 1000)
    assert (header["code"], header["reason"]) == ("image.decode_failed", "cpu")


# --- parent: edge cases ------------------------------------------------------------------------


def test_worker_reports_a_reject_with_a_non_image_code_as_a_failure() -> None:
    frame = protocol.encode_frame({"ok": False, "code": "other.thing", "reason": "r", "stage": "s"})
    script = f"import sys; sys.stdout.buffer.write({frame!r})"
    with pytest.raises(ImageRejected) as caught:
        run_worker("probe", {}, b"", argv=[sys.executable, "-c", script])
    assert caught.value.code == "image.worker_failed"


def test_worker_reply_over_the_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "MAX_REPLY_BYTES", 1000)
    script = "import sys; sys.stdout.buffer.write(b'x' * 100000)"
    with pytest.raises(ImageRejected) as caught:
        run_worker("probe", {}, b"", argv=[sys.executable, "-c", script])
    assert (caught.value.code, caught.value.reason) == ("image.worker_failed", "reply too large")


def test_worker_that_never_reads_its_input_does_not_hang_the_parent() -> None:
    script = "import sys; sys.stdout.buffer.write(b'')"
    with pytest.raises(ImageRejected):
        run_worker("probe", {}, b"x" * 5_000_000, argv=[sys.executable, "-c", script])


def test_sigkilled_worker_after_its_cpu_budget_is_a_cpu_failure() -> None:
    script = "import os, signal, time; time.sleep(1.2); os.kill(os.getpid(), signal.SIGKILL)"
    with pytest.raises(ImageRejected) as caught:
        run_worker(
            "probe",
            {},
            b"",
            limits=WorkerLimits(cpu_seconds=1),
            argv=[sys.executable, "-c", script],
        )
    assert (caught.value.code, caught.value.reason) == ("image.decode_failed", "cpu")


def test_worker_limits_follow_the_caps() -> None:
    small = WorkerLimits.for_caps(ImageCaps(max_pixels=1_000, max_bytes=10))
    big = WorkerLimits.for_caps(caps_for(ImageKind.WEBTOON))
    assert big.memory_bytes > small.memory_bytes > 0


def test_sandbox_mode_is_logged_as_a_warning_when_python_only() -> None:
    import structlog.testing

    client._mode_logged.clear()
    with structlog.testing.capture_logs() as logs:
        client._log_sandbox_mode(None)
        client._log_sandbox_mode("python")
    assert [(e["event"], e["log_level"]) for e in logs] == [("decode_worker_sandbox", "warning")]


@pytest.mark.parametrize(
    "kwargs",
    [{"max_pixels": 0, "max_bytes": 1}, {"max_pixels": 1, "max_bytes": 0},
     {"max_pixels": 1, "max_bytes": 1, "max_height": 0}],
)  # fmt: skip
def test_caps_must_be_positive(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="positive"):
        ImageCaps(**kwargs)


def test_thumbnail_argument_checks_do_not_start_a_worker(malicious: ModuleType) -> None:
    from shelfhold.infrastructure.imaging import make_thumbnail

    with pytest.raises(ValueError, match="max_edge"):
        make_thumbnail(b"", ImageKind.PAGE, max_edge=5000)


def test_protocol_decode_refuses_trailing_bytes() -> None:
    with pytest.raises(ValueError, match="mismatch"):
        protocol.decode_frame(protocol.encode_frame({}) + b"x", 50)


def test_the_worker_checks_the_pixel_cap_itself_if_pillow_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Image, "_decompression_bomb_check", lambda size: None)
    header, _ = _reply("probe", {"caps": {**CAPS, "max_pixels": 100}}, _png())
    assert (header["code"], header["reason"], header["stage"]) == (
        "image.too_large",
        "pixels",
        "header",
    )


def test_denied_socket_module_refuses_every_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = worker._DeniedModule("socket")
    with pytest.raises(PermissionError):
        denied.socket  # noqa: B018
    monkeypatch.setitem(sys.modules, "socket", denied)
    assert worker._python_socket() == "denied"


def test_libc_helpers_degrade_without_libc(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_libc(*_: Any, **__: Any) -> Any:
        raise OSError("no libc")

    monkeypatch.setattr(worker.ctypes, "CDLL", no_libc)
    assert worker._libc() is None
    assert worker._raw_connect(9) == "unavailable"


def test_raw_connect_reports_a_socket_that_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Libc:
        def socket(self, *_: int) -> int:
            return -1

    monkeypatch.setattr(worker, "_libc", lambda: Libc())
    assert worker._raw_connect(9) == "failed"


def test_lower_sets_soft_and_hard_together(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(worker.resource, "setrlimit", lambda which, v: calls.append((which, v)))
    worker._lower(7, 42)
    assert calls == [(7, (42, 42))]


def test_avif_thread_limit_tolerates_a_build_without_avif(monkeypatch: pytest.MonkeyPatch) -> None:
    import PIL

    monkeypatch.setitem(sys.modules, "PIL.AvifImagePlugin", None)
    monkeypatch.delattr(PIL, "AvifImagePlugin", raising=False)
    worker._single_thread_avif()  # ImportError swallowed


def test_deadline_already_passed_is_a_timeout() -> None:
    with pytest.raises(ImageRejected) as caught:
        run_worker(
            "probe",
            {},
            b"",
            limits=WorkerLimits(wall_seconds=0),
            argv=[sys.executable, "-c", "import time; time.sleep(5)"],
        )
    assert (caught.value.code, caught.value.reason) == ("image.decode_failed", "timeout")
