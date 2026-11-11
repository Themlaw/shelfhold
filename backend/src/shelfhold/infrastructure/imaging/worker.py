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
import platform
import resource
import signal
import struct
import sys
import types
from typing import IO, Any

from shelfhold.infrastructure.imaging.caps import ALLOWED_FORMATS
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


def _enter_network_namespace() -> bool:  # pragma: no cover - unshare changes the whole process
    libc = _libc()
    if libc is None or not hasattr(libc, "unshare"):
        return False
    if libc.unshare(CLONE_NEWNET) == 0:
        return True
    return bool(libc.unshare(CLONE_NEWUSER | CLONE_NEWNET) == 0)


# --- seccomp (no network, no new process) --------------------------------------------------------
# A classic BPF filter, written by hand: no libseccomp. Per architecture: (audit arch, socket,
# clone, clone3, other fork-like calls). Any other architecture gets no filter (mode `python`).
_SECCOMP_ARCHES = {
    "x86_64": (0xC000003E, 41, 56, 435, (57, 58)),  # fork, vfork
    "aarch64": (0xC00000B7, 198, 220, 435, ()),
}
_SECCOMP_SET_MODE_FILTER = 22
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_EPERM = 0x00050000 | 1
_RET_ENOSYS = 0x00050000 | 38  # clone3 -> glibc falls back to clone, which the filter checks
_BLOCKED_FAMILIES = (2, 10, 17)  # AF_INET, AF_INET6, AF_PACKET
_CLONE_THREAD = 0x00010000


def _bpf(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def seccomp_program(machine: str) -> bytes | None:
    """The filter for this architecture, or None when it is not covered.

    Refused with EPERM: socket(AF_INET | AF_INET6 | AF_PACKET), fork, vfork and clone without
    CLONE_THREAD (a thread is fine, a process is not). clone3 answers ENOSYS. A call made through
    another ABI than the native one kills the process.
    """
    if machine not in _SECCOMP_ARCHES:
        return None
    arch, socket_nr, clone_nr, clone3_nr, forks = _SECCOMP_ARCHES[machine]
    ld, jeq, jset, ret = 0x20, 0x15, 0x45, 0x06
    # (opcode, k, true-label, false-label); labels name the instruction to go to, None = next one
    prog: list[tuple[int, int, str | None, str | None, str | None]] = [
        (ld, 4, None, None, None),  # arch
        (jeq, arch, None, "kill", None),
        (ld, 0, None, None, None),  # syscall number
        (0x35, 0x40000000, "kill", None, None),  # jge: x32 ABI on x86_64 would dodge the numbers
        (jeq, socket_nr, "sock", None, None),
        (jeq, clone_nr, "clone", None, None),
        (jeq, clone3_nr, "enosys", None, None),
        *[(jeq, nr, "eperm", None, None) for nr in forks],
        (ret, _RET_ALLOW, None, None, None),
        (ld, 16, None, None, "sock"),  # args[0] of socket(): the family
        *[(jeq, fam, "eperm", None, None) for fam in _BLOCKED_FAMILIES],
        (ret, _RET_ALLOW, None, None, None),
        (ld, 16, None, None, "clone"),  # args[0] of clone(): the flags
        (jset, _CLONE_THREAD, "allow", "eperm", None),
        (ret, _RET_ALLOW, None, None, "allow"),
        (ret, _RET_EPERM, None, None, "eperm"),
        (ret, _RET_ENOSYS, None, None, "enosys"),
        (ret, _RET_KILL_PROCESS, None, None, "kill"),
    ]
    where = {label: i for i, (*_, label) in enumerate(prog) if label}
    out = []
    for i, (code, k, jt, jf, _label) in enumerate(prog):
        if code in (ret, ld):
            out.append(_bpf(code, 0, 0, k))
            continue
        t = where[jt] - i - 1 if jt else 0
        f = where[jf] - i - 1 if jf else 0
        out.append(_bpf(code, t, f, k))
    return b"".join(out)


class _SockFprog(ctypes.Structure):
    _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.c_void_p))


def _install_seccomp(
    libc: ctypes.CDLL | None,
) -> bool:  # pragma: no cover - a filter cannot be removed
    program = seccomp_program(platform.machine())
    if libc is None or program is None:
        return False
    buffer = ctypes.create_string_buffer(program)
    fprog = _SockFprog(len(program) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    return bool(libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) == 0)


def _deny_python_network() -> None:  # pragma: no cover - replaces sys.modules for good
    for name in ("socket", "_socket", "ssl", "_ssl"):
        sys.modules[name] = _DeniedModule(name)


def _lower(which: int, value: int) -> None:
    resource.setrlimit(which, (value, value))


def _sandbox(
    limits: dict[str, Any],
) -> dict[str, Any]:  # pragma: no cover - rlimits are irrevocable
    in_netns = bool(limits.get("netns", True)) and _enter_network_namespace()
    _deny_python_network()
    libc = _libc()
    no_new_privs = libc is not None and libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0
    seccomp = no_new_privs and _install_seccomp(libc)
    _lower(resource.RLIMIT_CORE, 0)
    _lower(resource.RLIMIT_AS, int(limits["memory_bytes"]))
    cpu = int(limits["cpu_seconds"])
    # soft limit: SIGXCPU, caught below; hard limit two seconds later: SIGKILL
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 2))
    signal.signal(signal.SIGXCPU, _on_cpu_limit)
    _lower(resource.RLIMIT_NOFILE, 64)
    _lower(resource.RLIMIT_FSIZE, 0)
    _lower(resource.RLIMIT_NPROC, 1)  # no thread beyond the main one, no process
    return {"isolation": "netns" if in_netns else "seccomp" if seccomp else "python",
            "seccomp": seccomp}  # fmt: skip


def _on_cpu_limit(signum: int, frame: types.FrameType | None) -> None:
    reply = encode_frame(
        {"ok": False, "code": "image.decode_failed", "reason": "cpu", "stage": "decode"}
    )
    os.write(1, reply)
    os._exit(0)


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
            "seccomp": state["seccomp"],
            "python_socket": _python_socket(),
            "raw_connect": _raw_connect(int(args["port"])),
        }
    if op == "limits":
        names = {
            "rlimit_as": resource.RLIMIT_AS,
            "rlimit_cpu": resource.RLIMIT_CPU,
            "rlimit_core": resource.RLIMIT_CORE,
            "rlimit_nofile": resource.RLIMIT_NOFILE,
            "rlimit_nproc": resource.RLIMIT_NPROC,
        }
        libc = _libc()
        no_new_privs = bool(
            libc is not None and libc.prctl(39, 0, 0, 0, 0) == 1
        )  # PR_GET_NO_NEW_PRIVS
        return {
            **{key: list(resource.getrlimit(which)) for key, which in names.items()},
            "no_new_privs": no_new_privs,
        }
    if op == "fork":  # pragma: no cover - forks the process; tested through the subprocess
        try:
            pid = os.fork()
        except OSError:
            return {"fork": "denied"}
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        return {"fork": "allowed"}
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
    formats = [name for name in caps["formats"] if name in ALLOWED_FORMATS and name in Image.OPEN]
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = int(caps["max_pixels"])
    try:
        with warnings.catch_warnings():  # the bomb warning is an error, here only
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data), formats=formats)
    except Image.DecompressionBombError as exc:  # beyond twice the cap: refused by Pillow itself
        raise _Reject("image.too_large", "pixels", "header") from exc
    except Image.DecompressionBombWarning as exc:
        raise _Reject("image.too_large", "pixels", "header") from exc
    except (UnidentifiedImageError, ValueError, SyntaxError, OSError) as exc:
        raise _Reject("image.format_refused", "not an allowlisted image", "header") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous
    width, height = image.size
    if width * height > caps["max_pixels"]:
        raise _Reject("image.too_large", "pixels", "header")
    if caps["max_height"] is not None and height > caps["max_height"]:
        raise _Reject("image.too_large", "height", "header")
    return image


def _guarded(call: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a decoding step; anything the decoder throws is a decode failure, never a crash."""
    from PIL import Image, UnidentifiedImageError

    try:
        return call(*args, **kwargs)
    except Image.DecompressionBombError as exc:
        raise _Reject("image.too_large", "pixels", "decode") from exc
    except MemoryError as exc:
        raise _Reject("image.decode_failed", "memory", "decode") from exc
    except (UnidentifiedImageError, ValueError, SyntaxError, OSError, EOFError,
            OverflowError, ZeroDivisionError, TypeError, KeyError, IndexError) as exc:  # fmt: skip
        raise _Reject("image.decode_failed", type(exc).__name__, "decode") from exc


def _single_thread_avif() -> None:
    """The sandbox allows no extra thread: the AVIF decoder must not ask for any."""
    try:
        from PIL import AvifImagePlugin
    except ImportError:  # no AVIF in this build: nothing to limit
        return
    AvifImagePlugin.DEFAULT_MAX_THREADS = 1


def _probe(data: bytes, args: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    _single_thread_avif()
    image = _open(data, args["caps"])
    info = {"format": image.format, "width": image.width, "height": image.height}
    _guarded(image.load)  # a full decode proves the data; the first frame only for animations
    return info, b""


def _thumbnail(data: bytes, args: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    from PIL import Image, ImageOps

    max_edge = int(args["max_edge"])
    fmt = THUMBNAIL_FORMATS[args["fmt"]]
    _single_thread_avif()
    image = _open(data, args["caps"])
    if image.format == "JPEG":  # decode at reduced size when possible: less memory, less time
        image.draft("RGB", (max_edge * 2, max_edge * 2))
    _guarded(image.load)
    image = _guarded(ImageOps.exif_transpose, image)  # orientation applied here, nowhere else
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    has_alpha = "A" in image.getbands() or "transparency" in image.info
    if fmt == "JPEG":
        image = image.convert("RGB")
    elif has_alpha and image.mode not in {"RGBA", "LA"}:
        image = image.convert("RGBA")
    elif image.mode not in {"RGB", "RGBA", "L", "LA"}:
        image = image.convert("RGB")
    image.info.clear()  # EXIF, ICC, text chunks, XMP: nothing from upstream is carried
    out = io.BytesIO()
    _guarded(image.save, out, fmt, icc_profile=None)
    return {"format": fmt, "width": image.width, "height": image.height}, out.getvalue()


def _pdf_check(data: bytes) -> tuple[dict[str, Any], bytes]:
    """Closed by default: `clean` only for a PDF the strict reader opens whole, with no encryption,
    no embedded file, no file attachment. Whatever it throws is a refusal, never a crash."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or "/Encrypt" in reader.trailer:
            return {"verdict": "protected", "reason": "pdf_encrypted"}, b""
        root: Any = reader.trailer["/Root"]
        names = root.get("/Names")
        if "/Collection" in root or (names is not None and "/EmbeddedFiles" in names.get_object()):
            return {"verdict": "refused", "reason": "embedded_file"}, b""
        for page in reader.pages:
            for annotation in page.get("/Annots") or []:
                if annotation.get_object().get("/Subtype") == "/FileAttachment":
                    return {"verdict": "refused", "reason": "file_attachment"}, b""
    except (Exception, RecursionError) as exc:
        return {"verdict": "refused", "reason": type(exc).__name__}, b""
    return {"verdict": "clean", "reason": "ok"}, b""


def _handle(
    op: str, args: dict[str, Any], data: bytes, state: dict[str, Any]
) -> tuple[dict[str, Any], bytes]:
    if op == "probe":
        return _probe(data, args)
    if op == "thumbnail":
        return _thumbnail(data, args)
    if op == "pdf_check":
        return _pdf_check(data)
    return _diagnose(op, args, state), b""


def reply_for(stdin: IO[bytes], state: dict[str, Any]) -> bytes:
    """Read one request frame, do it, and return the reply frame (an error is a reply too)."""
    try:
        header, data = read_frame(stdin, MAX_REQUEST_BYTES)
        result, payload = _handle(str(header["op"]), dict(header.get("args", {})), data, state)
        return encode_frame(
            {"ok": True, "result": result, "sandbox": state.get("isolation")}, payload
        )
    except _Reject as rejected:
        return encode_frame(
            {"ok": False, "code": rejected.code, "reason": rejected.reason, "stage": rejected.stage}
        )
    except MemoryError:
        return encode_frame(
            {"ok": False, "code": "image.decode_failed", "reason": "memory", "stage": "decode"}
        )


def main(argv: list[str]) -> int:  # pragma: no cover - the sandbox; run by the subprocess tests
    out = sys.stdout.buffer
    stdin = sys.stdin.buffer
    sys.stdout = sys.stderr  # nothing a library prints can corrupt the reply frame
    state = _sandbox(json.loads(argv[1]))
    out.write(reply_for(stdin, state))
    out.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point of the subprocess
    raise SystemExit(main(sys.argv))
