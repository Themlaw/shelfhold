# SPDX-License-Identifier: MIT
"""The direct-I/O detector of plugins (EF-SRC-002): a `sys.addaudithook`, **not** a sandbox.

A plugin never does network or disk I/O itself: it is handed the host's `Fetcher`. While plugin
code runs (`plugin_io`, or a call through `GuardedPlugin`), the audit hook looks at every socket
and name lookup, every process start, thread start, signal or foreign library, and every file or
directory outside the plugin's sandbox (code the plugin imports may be read). Each one is reported
with the plugin's id, even when the plugin catches an error, and an error inside the detector is
reported too (it never stays silent).

Two modes. `report` (the default, the host in production) only reports: the detector is not the
trust boundary, and refusing would break legitimate plugins (the time zone file read by
`datetime.now().astimezone()`, a temporary file) without closing the holes the process keeps.
`raise` (the conformance kit) refuses the event before it happens with `ForbiddenIOError`, which
fails the plugin's tests.

What the host does on the plugin's behalf (the `Fetcher` it was handed) runs in `host_io` and is
not looked at. The scope is a context variable: it follows the plugin into the tasks it creates,
and it is lost in a thread that already existed before the call. Hence a detector: Python code in
the host process can step around it (a pre-existing thread, an object of the host that does I/O,
memory tricks). A thread the plugin starts is seen when it starts, but what it then does is not:
a `threading.Thread` does not inherit the context variable. The isolation is the M6 sandbox.

Shared by the conformance kit (`raise`) and by the host (`shelfhold.plugin_host.audit_hook`,
`report`, which logs every event).
"""

import contextlib
import contextvars
import inspect
import os
import sys
import sysconfig
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

__all__ = [
    "ForbiddenIOError",
    "GuardedPlugin",
    "IoViolation",
    "allow_code_root",
    "host_io",
    "install",
    "installed",
    "plugin_io",
    "unwrap",
]


class ForbiddenIOError(PermissionError):
    """A plugin tried to do I/O itself instead of going through the Fetcher (EF-SRC-002)."""

    code: str = "source.forbidden_io"

    def __init__(self, plugin_id: str, event: str, detail: str = "") -> None:
        self.plugin_id = plugin_id
        self.event = event
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(
            f"plugin {plugin_id!r} attempted direct I/O ({event}{suffix}); "
            "a plugin goes through the Fetcher it is handed"
        )


@dataclass(frozen=True)
class IoViolation:
    plugin_id: str
    event: str
    detail: str


Report = Callable[[IoViolation], object]
Mode = Literal["report", "raise"]


@dataclass
class _Scope:
    plugin_id: str
    sandbox: str | None  # real path, or None: no file at all
    report: Report | None
    mode: Mode = "report"
    violations: list[IoViolation] = field(default_factory=list)


_SCOPE: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "shelfhold_plugin_io", default=None
)
_BUSY = threading.local()  # the hook's own work (realpath, the report) is not looked at
_LOCK = threading.Lock()
_INSTALLED: list[bool] = []
_EXTRA_CODE_ROOTS: set[str] = set()
_ROOTS_CACHE: dict[tuple[str, ...], tuple[str, ...]] = {}
_SILENT: Final = "\x00silent"  # not a finding (refused in raise mode): the bytecode cache

# Every event of these families is refused in plugin code, except the path events below.
_FAMILIES: Final = (
    "socket.", "subprocess.", "os.", "shutil.", "pty.", "ctypes.", "_ctypes", "_thread.",
    "_posixsubprocess", "urllib.", "http.", "ftplib.", "smtplib.", "imaplib.", "poplib.",
    "telnetlib.", "nntplib.", "webbrowser.", "sqlite3.", "glob.", "pathlib.", "tempfile.",
    "fcntl.", "mmap.", "signal.", "resource.", "syslog.", "winreg.", "msvcrt.", "_winapi.",
    "sys.addaudithook", "sys.remote_exec",
)  # fmt: skip
# Not I/O, never refused: `marshal.dumps` (the import system compiles a module the plugin imports,
# its cache write is what is refused), tracing and profiling (coverage, debuggers).
# Path events: allowed when every path is in the sandbox (reads: also code the plugin imports).
_READS: Final = frozenset({"os.listdir", "os.scandir", "os.listxattr", "os.getxattr"})
_WRITES: Final = frozenset(
    {
        "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.symlink", "os.link", "os.chmod",
        "os.chown", "os.utime", "os.truncate", "os.chflags", "os.lchflags", "os.setxattr",
        "os.removexattr", "shutil.copyfile", "shutil.copymode", "shutil.copystat",
        "shutil.copytree", "shutil.rmtree", "shutil.move",
    }
)  # fmt: skip
_CODE_SUFFIXES: Final = (".py", ".pyc", ".so", ".pyd")
_WRITE_FLAGS: Final = (
    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND | getattr(os, "O_TMPFILE", 0)
)


def install() -> None:
    """Add the audit hook once per process (it cannot be removed, and costs one lookup per
    audited event outside plugin code)."""
    with _LOCK:
        if _INSTALLED:
            return
        _code_roots()  # computed in host code, not on a plugin's first import
        sys.addaudithook(_hook)
        _INSTALLED.append(True)


def installed() -> bool:
    return bool(_INSTALLED)


def allow_code_root(path: str | os.PathLike[str]) -> None:
    """A directory plugin code may be imported from (the third-party `site-packages`)."""
    with _LOCK:
        _EXTRA_CODE_ROOTS.add(os.path.realpath(path))
        _ROOTS_CACHE.clear()


@contextlib.contextmanager
def plugin_io(
    plugin_id: str,
    *,
    sandbox: str | os.PathLike[str] | None = None,
    report: Report | None = None,
    mode: Mode = "report",
) -> Iterator[list[IoViolation]]:
    """Run the block as plugin code: direct I/O is reported (and refused in `raise` mode).
    Yields the violations."""
    if mode not in ("report", "raise"):
        raise ValueError(f"unknown detector mode {mode!r}")
    install()
    root = os.path.realpath(sandbox) if sandbox is not None else None
    scope = _Scope(plugin_id, root, report, mode)
    token = _SCOPE.set(scope)
    try:
        yield scope.violations
    finally:
        _SCOPE.reset(token)


@contextlib.contextmanager
def host_io() -> Iterator[None]:
    """Run the block as host code, inside a plugin call (the Fetcher's work)."""
    token = _SCOPE.set(None)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _hook(event: str, args: tuple[Any, ...]) -> None:
    scope = _SCOPE.get()
    if scope is None or getattr(_BUSY, "on", False):
        return
    if event != "open" and not event.startswith(_FAMILIES):
        return
    _BUSY.on = True
    try:
        try:
            refused = _verdict(event, args, scope)
        except Exception as exc:  # a detector that cannot decide reports (and refuses in raise)
            refused = f"the detector could not judge the event ({type(exc).__name__})"
        if refused is None:
            return
        if refused != _SILENT:
            violation = IoViolation(scope.plugin_id, event, refused)
            scope.violations.append(violation)
            if scope.report is not None:
                with contextlib.suppress(Exception):  # a failing report changes nothing
                    scope.report(violation)
    finally:
        _BUSY.on = False
    if scope.mode != "raise":
        return
    raise ForbiddenIOError(scope.plugin_id, event, "" if refused == _SILENT else refused)


def _verdict(event: str, args: tuple[Any, ...], scope: _Scope) -> str | None:
    """None when plugin code may do this; else why not (or `_SILENT`)."""
    if event == "open":
        path, mode, flags = (*args, None, None, None)[:3]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        )
        return _path_verdict([path], writes=writes, scope=scope, code_ok=True)
    if event in _READS:
        return _path_verdict(args[:1], writes=False, scope=scope, code_ok=True)
    if event in _WRITES:
        paths = args[:2] if event in {"os.rename", "os.symlink", "os.link"} else args[:1]
        if event.startswith("shutil.copy") or event == "shutil.move":
            paths = args[:2]
        return _path_verdict(paths, writes=True, scope=scope, code_ok=False)
    return f"{event} is not allowed in plugin code"


def _path_verdict(paths: Any, *, writes: bool, scope: _Scope, code_ok: bool) -> str | None:
    for raw in paths:
        if isinstance(raw, int) or raw is None:
            return "a file descriptor or an unnamed file"
        path = os.path.realpath(os.fsdecode(raw))
        if scope.sandbox is not None and _under(path, scope.sandbox):
            continue
        if writes:
            if f"{os.sep}__pycache__{os.sep}" in path or path.endswith(f"{os.sep}__pycache__"):
                return _SILENT  # the bytecode cache of an import: refused without a finding
            return f"write outside the sandbox: {path}"
        if code_ok and _is_code(path):
            continue
        return f"read outside the sandbox: {path}"
    return None


def _is_code(path: str) -> bool:
    """A module (or a directory of modules) under one of the import roots."""
    roots = _code_roots()
    if not any(_under(path, r) for r in roots):
        return False
    return path.endswith(_CODE_SUFFIXES) or os.path.isdir(path) or ".dist-info" in path


def _code_roots() -> tuple[str, ...]:
    key = tuple(sys.path)
    cached = _ROOTS_CACHE.get(key)
    if cached is not None:
        return cached
    found = {os.path.realpath(p) for p in key if p and os.path.isabs(p)}
    found |= {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    for name in ("stdlib", "platstdlib", "purelib", "platlib"):
        with contextlib.suppress(KeyError):
            found.add(os.path.realpath(sysconfig.get_paths()[name]))
    found |= _EXTRA_CODE_ROOTS
    found.discard(os.sep)  # a root of `/` would make every file "code"
    roots = tuple(sorted(found))
    _ROOTS_CACHE.clear()
    _ROOTS_CACHE[key] = roots
    return roots


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


# -- proxies ---------------------------------------------------------------------------------------


def _is_fetcher(value: Any) -> bool:
    return not isinstance(value, Mapping | type) and all(
        callable(getattr(value, n, None)) for n in ("get", "post", "cookies")
    )


class _HostSide:
    """The Fetcher seen from plugin code: every call it makes runs as host code."""

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._inner, name)
        if not callable(value):
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            with host_io():
                result = value(*args, **kwargs)
            if inspect.isawaitable(result):
                return _awaited(result, host_io)
            return result

        return call


async def _awaited(result: Any, scope: Callable[[], contextlib.AbstractContextManager[Any]]) -> Any:
    with scope():
        return await result


class GuardedPlugin:
    """A plugin whose every method runs under `plugin_io`; Fetcher arguments stay host code."""

    def __init__(
        self,
        inner: Any,
        plugin_id: str,
        *,
        sandbox: str | os.PathLike[str] | None = None,
        report: Report | None = None,
        mode: Mode = "report",
    ) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "_sandbox", sandbox)
        object.__setattr__(self, "_report", report)
        object.__setattr__(self, "_mode", mode)

    def _scope(self) -> contextlib.AbstractContextManager[Any]:
        return plugin_io(
            self.plugin_id, sandbox=self._sandbox, report=self._report, mode=self._mode
        )

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._inner, name)
        if not callable(value) or isinstance(value, type) or name.startswith("__"):
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            args = tuple(_HostSide(a) if _is_fetcher(a) else a for a in args)
            kwargs = {k: _HostSide(v) if _is_fetcher(v) else v for k, v in kwargs.items()}
            with self._scope():
                result = value(*args, **kwargs)
            if inspect.isawaitable(result):
                return _awaited(result, self._scope)
            return result

        return call

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    def __repr__(self) -> str:
        return f"GuardedPlugin({self._inner!r}, {self.plugin_id!r})"


def unwrap(value: Any) -> Any:
    """The plugin object behind a `GuardedPlugin` (identity checks)."""
    return object.__getattribute__(value, "_inner") if isinstance(value, GuardedPlugin) else value
