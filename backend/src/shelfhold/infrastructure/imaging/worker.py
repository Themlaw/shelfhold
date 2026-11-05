"""The decode worker: the only module that imports Pillow (ENF-SEC-003).

Run as `python -I -m shelfhold.infrastructure.imaging.worker '<limits json>'`: one request frame on
stdin, one reply frame on stdout, then exit. Before it reads a byte of the image, the process

* gets its own empty network namespace when the kernel allows it without privilege
  (`unshare(CLONE_NEWNET)`, through a user namespace if needed), and in every case replaces the
  `socket` module with a stub that refuses;
* sets `PR_SET_NO_NEW_PRIVS`;
* lowers its hard limits on address space, CPU time, open files, file size and core size, so
  nothing it runs afterwards can raise them again.

Limit of the Python-level fallback: it stops Python code, not a native decoder that calls the
`socket` system call itself. Only the network namespace stops that. The mode in force is reported
(`isolation`: `netns` or `python`) and documented in docs/security.

`ImageOps.exif_transpose` is called here and nowhere else (EXIF is an untrusted IFD; its parser
runs inside the limits). `Image.open` is always given the allowlist, so EPS (Ghostscript) and
every unlisted format are unreachable.
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import resource
import sys
import types
from typing import Any

from shelfhold.infrastructure.imaging.protocol import encode_frame, read_frame

CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000
PR_SET_NO_NEW_PRIVS = 38
MAX_REQUEST_BYTES = 512 * 1024 * 1024
THUMBNAIL_FORMATS = {"webp": "WEBP", "jpeg": "JPEG", "png": "PNG"}


class _Reject(Exception):
    def __init__(self, code: str, reason: str, stage: str) -> None:
        super().__init__(code)
        self.code, self.reason, self.stage = code, reason, stage


# --- sandbox -----------------------------------------------------------------------------------


class _DeniedModule(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        raise PermissionError(f"network is not available in the decode worker ({name})")


def _libc() -> ctypes.CDLL | None:
    try:
        return ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None


def _enter_network_namespace() -> bool:
    libc = _libc()
    if libc is None or not hasattr(libc, "unshare"):
        return False
    if libc.unshare(CLONE_NEWNET) == 0:
        return True
    return bool(libc.unshare(CLONE_NEWUSER | CLONE_NEWNET) == 0)


def _deny_python_network() -> None:
    for name in ("socket", "_socket", "ssl", "_ssl"):
        sys.modules[name] = _DeniedModule(name)


def _lower(which: int, value: int) -> None:
    resource.setrlimit(which, (value, value))


def _sandbox(limits: dict[str, Any]) -> dict[str, Any]:
    isolation = "netns" if limits.get("netns", True) and _enter_network_namespace() else "python"
    _deny_python_network()
    libc = _libc()
    if libc is not None:
        libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    _lower(resource.RLIMIT_CORE, 0)
    _lower(resource.RLIMIT_AS, int(limits["memory_bytes"]))
    _lower(resource.RLIMIT_CPU, int(limits["cpu_seconds"]))
    _lower(resource.RLIMIT_NOFILE, 64)
    _lower(resource.RLIMIT_FSIZE, 0)
    return {"isolation": isolation}


# --- diagnostics (read-only facts about the sandbox) ---------------------------------------------


def _raw_connect(port: int) -> str:
    """connect() to loopback through libc, around the Python stub: what a native bug could do."""
    libc = _libc()
    if libc is None:
        return "unavailable"
    fd = libc.socket(2, 1, 0)  # AF_INET, SOCK_STREAM
    if fd < 0:
        return "failed"
    try:
        address = ctypes.create_string_buffer(
            b"\x02\x00" + port.to_bytes(2, "big") + bytes([127, 0, 0, 1]) + b"\x00" * 8, 16
        )
        return "connected" if libc.connect(fd, address, 16) == 0 else "failed"
    finally:
        libc.close(fd)


def _python_socket() -> str:
    try:
        sys.modules["socket"].socket  # noqa: B018 - the stub raises on any attribute
    except PermissionError:
        return "denied"
    return "allowed"


def _diagnose(op: str, args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if op == "network":
        return {
            "isolation": state["isolation"],
            "python_socket": _python_socket(),
            "raw_connect": _raw_connect(int(args["port"])),
        }
    if op == "limits":
        names = {
            "rlimit_as": resource.RLIMIT_AS,
            "rlimit_cpu": resource.RLIMIT_CPU,
            "rlimit_core": resource.RLIMIT_CORE,
            "rlimit_nofile": resource.RLIMIT_NOFILE,
        }
        libc = _libc()
        no_new_privs = bool(
            libc is not None and libc.prctl(39, 0, 0, 0, 0) == 1
        )  # PR_GET_NO_NEW_PRIVS
        return {
            **{key: list(resource.getrlimit(which)) for key, which in names.items()},
            "no_new_privs": no_new_privs,
        }
    if op == "environment":
        return {
            "isolated": sys.flags.isolated == 1,
            "env_keys": sorted(os.environ),
            "cwd_is_empty_dir": os.listdir(".") == [],
            "pid": os.getpid(),
        }
    raise _Reject("image.worker_failed", f"unknown operation {op}", "worker")


# --- decoding ----------------------------------------------------------------------------------


def _open(data: bytes, caps: dict[str, Any]) -> Any:
    """Image.open on the allowlist, then the size checks, all before one pixel is decoded."""
    import warnings

    from PIL import Image, UnidentifiedImageError

    if len(data) > caps["max_bytes"]:
        raise _Reject("image.too_large", "bytes", "input")
    Image.init()
    formats = [name for name in caps["formats"] if name in Image.OPEN]
    Image.MAX_IMAGE_PIXELS = int(caps["max_pixels"])
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    try:
        image = Image.open(io.BytesIO(data), formats=formats)
    except Image.DecompressionBombError as exc:  # beyond twice the cap: refused by Pillow itself
        raise _Reject("image.too_large", "pixels", "header") from exc
    except Image.DecompressionBombWarning as exc:
        raise _Reject("image.too_large", "pixels", "header") from exc
    except (UnidentifiedImageError, ValueError, SyntaxError, OSError) as exc:
        raise _Reject("image.format_refused", "not an allowlisted image", "header") from exc
    width, height = image.size
    if width * height > caps["max_pixels"]:
        raise _Reject("image.too_large", "pixels", "header")
    if caps["max_height"] is not None and height > caps["max_height"]:
        raise _Reject("image.too_large", "height", "header")
    return image


def _guarded(call: Any, *args: Any) -> Any:
    """Run a decoding step; anything the decoder throws is a decode failure, never a crash."""
    from PIL import Image, UnidentifiedImageError

    try:
        return call(*args)
    except Image.DecompressionBombError as exc:
        raise _Reject("image.too_large", "pixels", "decode") from exc
    except MemoryError as exc:
        raise _Reject("image.decode_failed", "memory", "decode") from exc
    except (UnidentifiedImageError, ValueError, SyntaxError, OSError, EOFError,
            OverflowError, ZeroDivisionError, TypeError, KeyError, IndexError) as exc:  # fmt: skip
        raise _Reject("image.decode_failed", type(exc).__name__, "decode") from exc


def _probe(data: bytes, args: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    image = _open(data, args["caps"])
    info = {"format": image.format, "width": image.width, "height": image.height}
    _guarded(image.load)  # a full decode proves the data; the first frame only for animations
    return info, b""


def _thumbnail(data: bytes, args: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    from PIL import Image, ImageOps

    max_edge = int(args["max_edge"])
    fmt = THUMBNAIL_FORMATS[args["fmt"]]
    image = _open(data, args["caps"])
    if image.format == "JPEG":  # decode at reduced size when possible: less memory, less time
        image.draft("RGB", (max_edge * 2, max_edge * 2))
    _guarded(image.load)
    image = _guarded(ImageOps.exif_transpose, image)  # orientation applied here, nowhere else
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    has_alpha = "A" in image.getbands() or "transparency" in image.info
    if fmt == "JPEG":
        image = image.convert("RGB")
    elif image.mode not in {"RGB", "RGBA", "L", "LA"}:
        image = image.convert("RGBA" if has_alpha else "RGB")
    out = io.BytesIO()
    _guarded(image.save, out, fmt)  # no EXIF, no ICC, no text: nothing from upstream is carried
    return {"format": fmt, "width": image.width, "height": image.height}, out.getvalue()


def _handle(
    op: str, args: dict[str, Any], data: bytes, state: dict[str, Any]
) -> tuple[dict[str, Any], bytes]:
    if op == "probe":
        return _probe(data, args)
    if op == "thumbnail":
        return _thumbnail(data, args)
    return _diagnose(op, args, state), b""


def main(argv: list[str]) -> int:
    out = sys.stdout.buffer
    stdin = sys.stdin.buffer
    sys.stdout = sys.stderr  # nothing a library prints can corrupt the reply frame
    try:
        state = _sandbox(json.loads(argv[1]))
        header, data = read_frame(stdin, MAX_REQUEST_BYTES)
        result, payload = _handle(str(header["op"]), dict(header.get("args", {})), data, state)
        reply = encode_frame({"ok": True, "result": result}, payload)
    except _Reject as rejected:
        reply = encode_frame(
            {"ok": False, "code": rejected.code, "reason": rejected.reason, "stage": rejected.stage}
        )
    except MemoryError:
        reply = encode_frame(
            {"ok": False, "code": "image.decode_failed", "reason": "memory", "stage": "decode"}
        )
    out.write(reply)
    out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
