"""Installing third-party plugins (ENF-SEC-009, EF-SRC-019).

A plugin is a local wheel file with its sha256. It is installed with
``--only-binary :all: --require-hashes --no-deps`` (nothing is resolved, built or downloaded: the
command also says ``--no-index``) into a ``site-packages`` of its own under ``<data>/plugins``,
which is never on ``sys.path`` (``registry``: a finder after all others serves granted
plugins). Before any file is written the wheel is read as a zip and refused when:

- its distribution name, after PEP 503 normalisation, is the host's, the SDK's or one of their
  dependencies' (``shelfhold``, ``shelfhold-sdk``, ``httpx``...) or an installed plugin's;
- one of its top-level modules is a standard-library module, a module the host can import or a
  name already imported (a plugin can neither mask nor extend the host);
- it carries a ``.pth`` file, a ``.data`` directory, a path leaving the archive, or a
  ``*.dist-info`` that is not its own (a fake ``shelfhold-*.dist-info``).

The recorded state keeps, per plugin, the wheel hash and a digest of the installed files. The typed
confirmation (``enable --in-process``) is bound to that digest: reinstalling or editing the files
voids it. Since M6 (D13) the recorded state also keeps the **mode** of each plugin — isolated by
default, in process only on the admin's explicit word, never the plugin's — and the activation of
each mode: the typed confirmation for the host process, the reserved action (a fresh admin session
or the host CLI) for the isolated one. A plugin runs in one mode at a time.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib import metadata
from importlib.machinery import PathFinder
from pathlib import Path, PurePosixPath
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from shelfhold.infrastructure.archives import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import metadata as schema
from shelfhold.infrastructure.secrets.atomic import write_private
from shelfhold.plugin_host.registry import (
    ENTRY_POINT_GROUP,
)
from shelfhold.plugin_host.registry import (
    normalise_name as normalise_name,
)

ENV_ALLOW = "SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS"
CONFIRMATION = "mêmes droits que Shelfhold"
# D13 (M6 L06): a third-party plugin runs isolated by default; the in-process mode is the
# admin's explicit choice, never the plugin's (the manifest cannot ask for it).
MODE_ISOLATED = "isolated"
MODE_IN_PROCESS = "in_process"
MODES = (MODE_ISOLATED, MODE_IN_PROCESS)
RESERVED_DISTRIBUTIONS = frozenset({"shelfhold", "shelfhold-sdk", "httpx", "httpcore"})
# M6 L02 (D10): the SDK is its own distribution (`shelfhold_sdk`); a plugin shipping that
# top-level module would shadow it, so the name is reserved like the host's own.
RESERVED_MODULES = frozenset(
    {"shelfhold", "shelfhold_sdk", "httpx", "httpcore"}
)  # whatever is installed here
PIP_FLAGS = ("--only-binary", ":all:", "--require-hashes", "--no-deps", "--no-index")
NO_COMPILE = {
    "pip": "--no-compile",
    "uv": "--no-compile-bytecode",
}  # nothing is compiled at install
# the installer reads no configuration of the machine it runs on (uv.toml, pip.conf, the
# environment): the command says everything, the machine nothing
NO_CONFIG = {
    "pip": "--isolated",
    "uv": "--no-config",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIST_INFO = re.compile(r"^(?P<name>.+)-(?P<version>[^-]+)\.dist-info$")

# The installer also receives the folder to run it from: never the process's own cwd — from a
# workspace, uv merges the project's own dependencies into `--require-hashes` and refuses them.
Runner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]


class PluginInstallError(Exception):
    """A refusal with a stable code (`conflict`, `bad_wheel`, `hash_mismatch`...)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# -- layout and recorded state -----------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    root: Path

    @classmethod
    def of(cls, data_dir: Path) -> Layout:
        return cls(data_dir / "plugins")

    @property
    def site(self) -> Path:
        return self.root / "site-packages"

    @property
    def state(self) -> Path:
        return self.root / "state.json"


@dataclass(frozen=True)
class Installed:
    name: str  # PEP 503
    version: str
    wheel_sha256: str
    top_levels: tuple[str, ...]
    dist_info: str
    digest: str
    mode: str = MODE_ISOLATED  # D13: isolated by default, the admin chooses, never the plugin


@dataclass(frozen=True)
class Confirmation:
    digest: str  # of the installed files at the time of the typed confirmation
    at: str


@dataclass
class PluginFile:
    installed: dict[str, Installed]
    confirmed: dict[str, Confirmation]  # the typed phrase, for the in-process mode
    enabled: dict[str, Confirmation] = field(default_factory=dict)  # the reserved action, isolated


def read_state(layout: Layout) -> PluginFile:
    try:
        raw = json.loads(layout.state.read_text(encoding="utf-8"))
        installed = {
            n: Installed(
                name=n,
                version=str(v["version"]),
                wheel_sha256=str(v["wheel_sha256"]),
                top_levels=tuple(v["top_levels"]),
                dist_info=str(v["dist_info"]),
                digest=str(v["digest"]),
                mode=str(v.get("mode", MODE_ISOLATED)),
            )
            for n, v in raw.get("installed", {}).items()
        }
        confirmed = {
            n: Confirmation(str(v["digest"]), str(v["at"]))
            for n, v in raw.get("confirmed", {}).items()
        }
        enabled = {
            n: Confirmation(str(v["digest"]), str(v["at"]))
            for n, v in raw.get("enabled", {}).items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return PluginFile({}, {})  # unreadable or absent: nothing is installed, nothing confirmed
    return PluginFile(installed, confirmed, enabled)


def write_state(layout: Layout, state: PluginFile) -> None:
    layout.root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "installed": {
            n: {
                "version": i.version,
                "wheel_sha256": i.wheel_sha256,
                "top_levels": list(i.top_levels),
                "dist_info": i.dist_info,
                "digest": i.digest,
                "mode": i.mode,
            }
            for n, i in sorted(state.installed.items())
        },
        "confirmed": {
            n: {"digest": c.digest, "at": c.at} for n, c in sorted(state.confirmed.items())
        },
        "enabled": {n: {"digest": c.digest, "at": c.at} for n, c in sorted(state.enabled.items())},
    }
    write_private(layout.state, json.dumps(payload, indent=1).encode("utf-8"), overwrite=True)


def tree_digest(site: Path, entries: Sequence[str]) -> str:
    """Digest of every file under the given entries, with no exclusion: a `__pycache__` or a
    `.pyc` is a file like another (a planted unchecked-hash `.pyc` runs without its source).
    Symbolic links are hashed as links, never followed."""
    h = hashlib.sha256()
    for entry in sorted(entries):
        base = site / entry
        files = [base] if not base.is_dir() else sorted(base.rglob("*"))
        for path in files:
            if path.is_dir() and not path.is_symlink():
                continue
            rel = path.relative_to(site).as_posix()
            h.update(rel.encode() + b"\0")
            if path.is_symlink():
                h.update(b"link:" + os.readlink(path).encode())
            elif path.exists():
                h.update(hashlib.sha256(path.read_bytes()).digest())
            else:
                h.update(b"missing")
    return h.hexdigest()


def unexpected_entries(layout: Layout, state: PluginFile) -> list[str]:
    """What sits in the third-party directory without belonging to an installed plugin."""
    if not layout.site.is_dir():
        return []
    expected = {
        n for i in state.installed.values() for n in on_disk(layout.site, i.top_levels, i.dist_info)
    }
    return sorted(p.name for p in layout.site.iterdir() if p.name not in expected)


def safe_component(name: str) -> bool:
    """One path component: no separator, no `..`, not empty, not hidden from a plain listing."""
    return (
        bool(name)
        and name not in (".", "..")
        and "/" not in name
        and "\\" not in name
        and "\0" not in name
    )


def state_is_sound(layout: Layout, plugin: Installed) -> bool:
    """`state.json` is a file on disk like another: what it names must stay under the directory."""
    names = [
        *plugin.top_levels,
        f"{plugin.top_levels[0]}.py" if plugin.top_levels else "",
        plugin.dist_info,
    ]
    if not plugin.top_levels or not all(safe_component(n) for n in names):
        return False
    root = layout.site.resolve()
    return all((root / n).resolve().parent == root for n in names)


def on_disk(site: Path, tops: Sequence[str], dist_info: str) -> list[str]:
    """Names in `site` for the modules (a package directory or a single `.py`) and the dist-info."""
    names = [t if (site / t).is_dir() else f"{t}.py" for t in tops]
    return [*names, dist_info]


def current_digest(layout: Layout, plugin: Installed) -> str:
    return tree_digest(layout.site, on_disk(layout.site, plugin.top_levels, plugin.dist_info))


# -- reading the wheel -------------------------------------------------------------------------


@dataclass(frozen=True)
class WheelInfo:
    name: str  # PEP 503
    version: str
    top_levels: tuple[str, ...]
    dist_info: str
    tags: str  # the filename's tag section its own WHEEL metadata declares (PEP 425)


def _tag_section(declared: list[str]) -> str:
    """The tag section of a filename for the tags a wheel declares (PEP 425): the python tags
    compress into one when the abi and the platform are shared, else the first declared tag
    stands in the name — the publisher's own first word, never a made-up section."""
    split = [tag.rsplit("-", 2) for tag in declared]
    if any(len(parts) != 3 for parts in split):
        raise PluginInstallError("bad_wheel", "a declared wheel tag is not py-abi-platform")
    if len({(abi, platform) for _, abi, platform in split}) == 1:
        return "-".join((".".join(py for py, _, _ in split), split[0][1], split[0][2]))
    return declared[0]


def _top_level(path: str) -> str:
    first = PurePosixPath(path).parts[0]
    for suffix in sorted(importlib.machinery.all_suffixes(), key=len, reverse=True):
        if first.endswith(suffix):
            return first[: -len(suffix)]
    return first


_STARTUP_HOOKS = frozenset({"sitecustomize", "usercustomize"})


def _refuse_content(n: str, parts: tuple[str, ...]) -> None:
    """Content a plugin has no use for and the host must not unpack."""
    first = parts[0]
    if first.endswith((".egg-info", ".egg-link")):
        raise PluginInstallError("bad_wheel", f"{first!r}: egg-info refused (fake metadata)")
    if "__pycache__" in parts or n.endswith((".pyc", ".pyo")):
        raise PluginInstallError("bad_wheel", f"{n!r}: bytecode refused (sources only)")
    if first.endswith(".dist-info"):
        return
    top = _top_level(n)
    if top in _STARTUP_HOOKS:
        raise PluginInstallError("bad_wheel", f"{top!r}: start-up hook refused")
    if len(parts) == 1 and first != f"{top}.py":
        raise PluginInstallError("bad_wheel", f"{first!r}: top-level file that is not a module")
    if not top.isidentifier() or "." in first.removesuffix(".py"):
        raise PluginInstallError("bad_wheel", f"{first!r}: not a plain module or package name")
    if n.endswith((*importlib.machinery.EXTENSION_SUFFIXES, ".so", ".pyd")):
        raise PluginInstallError("bad_wheel", f"{n!r}: compiled modules refused (sources only)")


def inspect_wheel(wheel: Path) -> WheelInfo:
    if wheel.suffix != ".whl":
        raise PluginInstallError("bad_wheel", "not a .whl file")
    try:
        # A wheel is a ZIP; the opener knows it by extension, so the stream is given as `.zip`.
        with (
            wheel.open("rb") as stream,
            safe_open_archive(stream, name="wheel.zip") as archive,  # audited, never extracted
        ):
            names = [e.name for e in archive.entries if not e.is_dir]
            return _inspect_names(names, lambda n: bytes(archive.read(n)))
    except (UnsafeArchiveError, OSError, KeyError) as exc:
        raise PluginInstallError("bad_wheel", str(exc)) from exc


def _inspect_names(names: list[str], read: Callable[[str], bytes]) -> WheelInfo:
    for n in names:
        parts = PurePosixPath(n).parts
        if n.startswith("/") or ".." in parts or "\\" in n or not parts:
            raise PluginInstallError("bad_wheel", f"unsafe path {n!r}")
        if n.lower().endswith(".pth"):
            raise PluginInstallError("bad_wheel", f".pth file {n!r} (never executed)")
        if parts[0].endswith(".data"):
            raise PluginInstallError("bad_wheel", f"{parts[0]!r}: data directories refused")
        _refuse_content(n, parts)
    infos = {
        PurePosixPath(n).parts[0] for n in names if PurePosixPath(n).parts[0].endswith(".dist-info")
    }
    if len(infos) != 1:
        raise PluginInstallError("bad_wheel", f"{len(infos)} dist-info directories")
    dist_info = infos.pop()
    match = _DIST_INFO.match(dist_info)
    if match is None:
        raise PluginInstallError("bad_wheel", f"bad dist-info name {dist_info!r}")
    try:
        meta = read(f"{dist_info}/METADATA").decode("utf-8", "replace")
    except KeyError as exc:
        raise PluginInstallError("bad_wheel", "no METADATA") from exc
    declared = re.search(r"^Name:\s*(\S+)\s*$", meta, re.MULTILINE)
    if declared is None:
        raise PluginInstallError("bad_wheel", "METADATA declares no Name")
    name = normalise_name(declared.group(1))
    if normalise_name(match.group("name")) != name:
        raise PluginInstallError("bad_wheel", "dist-info name differs from METADATA Name")
    try:
        wheel_meta = read(f"{dist_info}/WHEEL").decode("utf-8", "replace")
    except KeyError as exc:
        raise PluginInstallError("bad_wheel", "no WHEEL metadata") from exc
    tags = [
        line.removeprefix("Tag:").strip()
        for line in wheel_meta.splitlines()
        if line.startswith("Tag:")
    ]
    if not tags:
        raise PluginInstallError("bad_wheel", "the WHEEL metadata declares no Tag")
    tops = sorted(
        {_top_level(n) for n in names if not PurePosixPath(n).parts[0].endswith(".dist-info")}
    )
    if not tops:
        raise PluginInstallError("bad_wheel", "no module")
    return WheelInfo(name, match.group("version"), tuple(tops), dist_info, _tag_section(tags))


def wheel_plugin_keys(wheel: Path) -> frozenset[str]:
    """The plugin keys the wheel's entry points declare (the ``shelfhold.plugins`` group), read
    from the archive alone: nothing is imported and nothing is installed. An install from an
    index requires these to be exactly the key the index entry names — a wheel that answers
    under another key than the one vouchsafed for it is not installed (M6 L08)."""
    try:
        # A wheel is a ZIP; the opener knows it by extension, so the stream is given as `.zip`.
        with (
            wheel.open("rb") as stream,
            safe_open_archive(stream, name="wheel.zip") as archive,  # audited, never extracted
        ):
            names = [e.name for e in archive.entries if not e.is_dir]
            return _entry_point_keys(names, lambda n: bytes(archive.read(n)))
    except (UnsafeArchiveError, OSError, KeyError, ValueError, TypeError) as exc:
        # a malformed entry_points.txt is a refused wheel, not a crash
        raise PluginInstallError("bad_wheel", str(exc)) from exc


def _read_in(read: Callable[[str], bytes], info: str, filename: str) -> bytes:
    return read(f"{info}/{filename}")


class _ArchivedDistribution(metadata.Distribution):
    """A distribution read from a wheel that is not installed: its files come from the archive,
    nothing is located on disk."""

    def __init__(self, read: Callable[[str], bytes]) -> None:
        self._read = read

    def read_text(self, filename: str) -> str | None:
        try:
            return self._read(filename).decode("utf-8", "replace")
        except KeyError:
            return None

    def locate_file(self, path: str | os.PathLike[str]) -> Path:
        raise FileNotFoundError(path)  # never installed: no file of it is on disk


def _entry_point_keys(names: list[str], read: Callable[[str], bytes]) -> frozenset[str]:
    """The names of the entry points of the plugin group, one per line of the section, in every
    dist-info of the archive (the guard refuses a wheel with more than one at install time)."""
    keys: set[str] = set()
    infos = sorted({PurePosixPath(n).parts[0] for n in names if n.endswith("/entry_points.txt")})
    for info in infos:
        # parsed by the very reader the loader uses (`dist.entry_points`), so no header or line a
        # hand-written parser would read otherwise can hide a key from this check
        dist = _ArchivedDistribution(functools.partial(_read_in, read, info))
        keys.update(ep.name for ep in dist.entry_points if ep.group == ENTRY_POINT_GROUP)
    return frozenset(keys)


# -- conflicts ---------------------------------------------------------------------------------


def host_path(layout: Layout) -> list[str]:
    """`sys.path` without the third-party directory."""
    site = str(layout.site)
    return [p for p in sys.path if p != site]


def host_distribution_names(layout: Layout) -> set[str]:
    names = set(RESERVED_DISTRIBUTIONS)
    for dist in metadata.distributions(path=host_path(layout)):
        raw = dist.metadata["Name"] if dist.metadata else None
        if isinstance(raw, str):
            names.add(normalise_name(raw))
    try:  # the host's own declared dependencies, installed or not
        for requirement in metadata.requires("shelfhold") or []:
            match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
            if match:
                names.add(normalise_name(match.group(0)))
    except metadata.PackageNotFoundError:
        pass
    return names


def module_conflict(top: str, layout: Layout) -> str | None:
    if top in RESERVED_MODULES or top in sys.stdlib_module_names or top in sys.builtin_module_names:
        return f"module {top!r} belongs to the host or the standard library"
    if top in sys.modules and not _from_site(top, layout):
        return f"module {top!r} is already imported by the host"
    if PathFinder.find_spec(top, host_path(layout)) is not None:
        return f"module {top!r} is importable from the host's own path"
    return None


def _from_site(top: str, layout: Layout) -> bool:
    origin = getattr(sys.modules.get(top), "__file__", None) or ""
    return str(origin).startswith(str(layout.site))


def check_conflicts(info: WheelInfo, layout: Layout, state: PluginFile) -> None:
    if info.name in host_distribution_names(layout):
        raise PluginInstallError("conflict", f"distribution {info.name!r} is the host's")
    if info.name in state.installed:
        raise PluginInstallError(
            "conflict", f"{info.name!r} is already installed (remove it first)"
        )
    for top in info.top_levels:
        reason = module_conflict(top, layout)
        if reason:
            raise PluginInstallError("conflict", reason)
    others = {t: n for n, i in state.installed.items() for t in i.top_levels}
    for top in info.top_levels:
        if top in others:
            raise PluginInstallError(
                "conflict", f"module {top!r} belongs to plugin {others[top]!r}"
            )


# -- the command -------------------------------------------------------------------------------


def installer_prefix() -> list[str]:
    """`python -m pip` when this interpreter has pip, otherwise `uv pip`."""
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install"]
    uv = shutil.which("uv") or os.environ.get("UV")  # `uv run` exports its own path as UV
    if uv is None or not Path(uv).exists():
        raise PluginInstallError("no_installer", "neither pip nor uv is available")
    return [uv, "pip", "install", "--python", sys.executable]


def install_command(prefix: Sequence[str], target: Path, requirements: Path) -> list[str]:
    tool = "pip" if "-m" in prefix else "uv"
    return [
        *prefix,
        "--target",
        str(target),
        *PIP_FLAGS,
        NO_COMPILE[tool],
        NO_CONFIG[tool],
        "-r",
        str(requirements),
    ]


def requirement_line(name: str, wheel: Path, sha256: str) -> str:
    return f"{name} @ {wheel.resolve().as_uri()} --hash=sha256:{sha256}\n"


def _run(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, local file only
        list(argv), capture_output=True, text=True, check=False, timeout=300, cwd=cwd
    )


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install_wheel(
    layout: Layout,
    wheel: Path,
    sha256: str,
    *,
    runner: Runner = _run,
    prefix: Sequence[str] | None = None,
) -> Installed:
    expected = sha256.lower().removeprefix("sha256:")
    if not _SHA256.match(expected):
        raise PluginInstallError("hash_invalid", "expected a 64-digit sha256")
    try:
        actual = file_sha256(wheel)
    except OSError as exc:
        raise PluginInstallError("bad_wheel", str(exc)) from exc
    if actual != expected:
        raise PluginInstallError("hash_mismatch", f"the file hashes to {actual}")
    info = inspect_wheel(wheel)
    state = read_state(layout)
    check_conflicts(info, layout, state)
    layout.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = layout.root / f".staging-{new_ulid()}"
    staging.mkdir(mode=0o700)
    try:
        requirements = staging / "requirements.txt"
        requirements.write_text(requirement_line(info.name, wheel, expected), encoding="utf-8")
        target = staging / "target"
        argv = install_command(
            prefix if prefix is not None else installer_prefix(), target, requirements
        )
        done = runner(argv, staging)
        if done.returncode != 0:
            raise PluginInstallError("install_failed", (done.stderr or done.stdout).strip()[-400:])
        _check_staged(target, info)
        layout.site.mkdir(parents=True, exist_ok=True, mode=0o700)
        moved: list[Path] = []
        try:
            for entry in on_disk(target, info.top_levels, info.dist_info):
                if (layout.site / entry).exists() or (layout.site / entry).is_symlink():
                    raise PluginInstallError("conflict", f"{entry!r} already exists")
                os.replace(target / entry, layout.site / entry)
                moved.append(layout.site / entry)
            open_for_runner(layout, moved)
        except OSError as exc:
            _undo(moved)
            raise PluginInstallError("bad_wheel", f"cannot place the files: {exc}") from exc
        except PluginInstallError:
            _undo(moved)
            raise
        digest = tree_digest(layout.site, on_disk(layout.site, info.top_levels, info.dist_info))
        installed = Installed(
            info.name, info.version, expected, info.top_levels, info.dist_info, digest
        )
        state.installed[info.name] = installed
        state.confirmed.pop(info.name, None)  # a reinstall is new code nobody vouched for
        state.enabled.pop(info.name, None)
        write_state(layout, state)
        return installed
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def open_for_runner(layout: Layout, placed: Sequence[Path]) -> None:
    """Make what the core put down readable by the plugin runner (D-P1). The core runs with
    `umask 077` and the companion is another uid: without this the companion could not enter the
    folder. Others get to read and traverse, never to write; the state file stays the core's own."""
    layout.root.chmod(0o755)
    layout.site.chmod(0o755)
    for entry in placed:
        paths = [entry, *entry.rglob("*")] if entry.is_dir() and not entry.is_symlink() else [entry]
        for path in paths:
            if path.is_symlink():
                continue  # a link is never followed: its own mode is not the point
            if path.is_dir():
                path.chmod(0o755)
            else:
                path.chmod(0o755 if path.stat().st_mode & 0o100 else 0o644)


def _undo(moved: Sequence[Path]) -> None:
    for path in moved:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _check_staged(target: Path, info: WheelInfo) -> None:
    """What the installer put down is what the wheel declared, nothing else (no `bin/`, no extra
    dist-info, no `.pth`)."""
    allowed = {
        info.dist_info,
        *info.top_levels,
        *(f"{t}.py" for t in info.top_levels),
        ".lock",  # uv --target
    }
    extra = sorted(p.name for p in target.iterdir() if p.name not in allowed)
    if extra:
        raise PluginInstallError("bad_wheel", f"unexpected files installed: {extra}")


def remove_plugin(layout: Layout, name: str) -> Installed:
    state = read_state(layout)
    key = normalise_name(name)
    plugin = state.installed.get(key)
    if plugin is None:
        raise PluginInstallError("unknown", f"{name!r} is not installed")
    if not state_is_sound(layout, plugin):
        raise PluginInstallError(
            "bad_state", "state.json names a path outside the plugin directory"
        )
    for entry in on_disk(layout.site, plugin.top_levels, plugin.dist_info):
        path = layout.site / entry
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    del state.installed[key]
    state.confirmed.pop(key, None)
    state.enabled.pop(key, None)
    write_state(layout, state)
    return plugin


# -- the kept wheels of the lifecycle (M6 L09a, EF-SRC-020, D15) ------------------------------


def kept_wheel_path(layout: Layout, key: str) -> Path:
    """The wheel of the installed version, kept for the day the plugin is updated: the previous
    version is the one — and the only one — the reserved rollback returns to."""
    return layout.root / "wheels" / f"{key}.whl"


def previous_wheel_path(layout: Layout, key: str) -> Path:
    """The wheel of the previous kept version, moved aside by an update, gone once the rollback
    has put it back."""
    return layout.root / "previous" / f"{key}.whl"


def keep_wheel(layout: Layout, key: str, wheel: Path, dest: Path | None = None) -> Path:
    """Keep a copy of a verified wheel under the plugins volume (never on ``sys.path``, never in
    ``site-packages``: the guard's stray check does not see it). The caller has verified the
    wheel; the copy is byte for byte the one the index vouched for. ``dest`` stages the copy
    under another name first, for an act that must not leave anything fallible after its line
    is committed (M6 L09a)."""
    kept = dest if dest is not None else kept_wheel_path(layout, key)
    kept.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    kept.write_bytes(wheel.read_bytes())
    return kept


def drop_kept_wheels(layout: Layout, key: str) -> None:
    """An uninstall leaves none of the plugin's code behind: the kept wheels go with the files."""
    for kept in (kept_wheel_path(layout, key), previous_wheel_path(layout, key)):
        # a staged copy (M6 L09a) a dead process left beside a kept wheel is the plugin's code too
        for path in (kept, kept.with_name(f"{kept.name}.next")):
            if path.exists():
                path.unlink()


# -- activation --------------------------------------------------------------------------------


def env_allows(environ: dict[str, str] | os._Environ[str]) -> bool:
    return environ.get(ENV_ALLOW) == "1"


def set_mode(layout: Layout, name: str, mode: str) -> Installed:
    """The admin chooses the mode (D13): isolated by default, in process on an explicit word.
    A change of mode voids both activations: each mode has its own grant."""
    if mode not in MODES:
        raise PluginInstallError(
            "mode_invalid", f"the mode is {MODE_ISOLATED!r} or {MODE_IN_PROCESS!r}"
        )
    state = read_state(layout)
    key = normalise_name(name)
    plugin = state.installed.get(key)
    if plugin is None:
        raise PluginInstallError("unknown", f"{name!r} is not installed")
    if plugin.mode != mode:
        plugin = replace(plugin, mode=mode)
        state.installed[key] = plugin
        state.confirmed.pop(key, None)
        state.enabled.pop(key, None)
        write_state(layout, state)
    return plugin


def _sound_and_unchanged(layout: Layout, plugin: Installed, strays: list[str]) -> str | None:
    """What both modes check before a plugin may run: the state names nothing outside the
    directory, no file nobody installed sits in it, and the files are the recorded ones."""
    if not state_is_sound(layout, plugin):
        return "state.json names a path outside the plugin directory"
    if strays:  # a file nobody installed voids every activation
        return f"unexpected files in site-packages: {strays}"
    return None


def enable_isolated(layout: Layout, name: str) -> Confirmation:
    """The reserved action of D13 (a fresh admin session or the host CLI): the plugin may run
    isolated. No variable, no typed sentence — and never the in-process mode."""
    state = read_state(layout)
    key = normalise_name(name)
    plugin = state.installed.get(key)
    if plugin is None:
        raise PluginInstallError("unknown", f"{name!r} is not installed")
    if plugin.mode != MODE_ISOLATED:
        raise PluginInstallError(
            "mode_invalid",
            f"{name!r} is not in isolated mode: the reserved action enables an isolated plugin",
        )
    strays = unexpected_entries(layout, state)
    problem = _sound_and_unchanged(layout, plugin, strays)
    if problem is not None:
        raise PluginInstallError("bad_state", problem)
    digest = current_digest(layout, plugin)
    if digest != plugin.digest:
        raise PluginInstallError("modified", "installed files differ from the recorded install")
    enabled = Confirmation(digest, datetime.now(UTC).isoformat())
    state.enabled[key] = enabled
    state.confirmed.pop(key, None)  # one mode at a time
    write_state(layout, state)
    return enabled


def confirm(layout: Layout, name: str, typed: str) -> Confirmation:
    """Record the typed confirmation for the files as they are now (the in-process mode)."""
    state = read_state(layout)
    key = normalise_name(name)
    plugin = state.installed.get(key)
    if plugin is None:
        raise PluginInstallError("unknown", f"{name!r} is not installed")
    if typed.strip() != CONFIRMATION:
        raise PluginInstallError("confirmation_mismatch")
    if not state_is_sound(layout, plugin):
        raise PluginInstallError(
            "bad_state", "state.json names a path outside the plugin directory"
        )
    digest = current_digest(layout, plugin)
    if digest != plugin.digest:
        raise PluginInstallError("modified", "installed files differ from the recorded install")
    confirmation = Confirmation(digest, datetime.now(UTC).isoformat())
    state.confirmed[key] = confirmation
    state.enabled.pop(key, None)  # one mode at a time
    write_state(layout, state)
    return confirmation


def unconfirm(layout: Layout, name: str) -> None:
    state = read_state(layout)
    key = normalise_name(name)
    state.confirmed.pop(key, None)
    state.enabled.pop(key, None)
    write_state(layout, state)


@dataclass(frozen=True)
class Verdict:
    name: str
    granted: bool
    reason: str | None = None
    mode: str = MODE_ISOLATED

    @property
    def loads_in_process(self) -> bool:
        """Only the in-process mode ever loads in the host process: an isolated plugin is the
        companion's to run, whatever the environment allows."""
        return self.granted and self.mode == MODE_IN_PROCESS


def verdicts(layout: Layout, environ: dict[str, str] | os._Environ[str]) -> list[Verdict]:
    """For each installed plugin, in its own mode: granted only when the activation of that
    mode holds (the reserved action for isolated, the variable and the typed confirmation for
    in process). Each condition is checked again at every start; none is remembered on its own."""
    state = read_state(layout)
    strays = unexpected_entries(layout, state)
    out = []
    for name, plugin in sorted(state.installed.items()):
        problem = _sound_and_unchanged(layout, plugin, strays)
        if plugin.mode == MODE_IN_PROCESS:
            confirmation = state.confirmed.get(name)
            if not env_allows(environ):
                out.append(Verdict(name, False, f"{ENV_ALLOW}=1 is not set", plugin.mode))
            elif confirmation is None:
                out.append(
                    Verdict(
                        name, False, "no typed confirmation (shelfhold plugin enable)", plugin.mode
                    )
                )
            elif problem is not None:
                out.append(Verdict(name, False, problem, plugin.mode))
            elif (
                confirmation.digest != plugin.digest
                or current_digest(layout, plugin) != plugin.digest
            ):
                out.append(
                    Verdict(name, False, "files changed since the confirmation", plugin.mode)
                )
            else:
                out.append(Verdict(name, True, None, plugin.mode))
        else:  # isolated (the default): enabling is a reserved action, nothing is typed
            enabled = state.enabled.get(name)
            if enabled is None:
                out.append(
                    Verdict(
                        name,
                        False,
                        "not enabled: activating an isolated plugin is a reserved action "
                        "(a fresh admin session or the host CLI)",
                        plugin.mode,
                    )
                )
            elif problem is not None:
                out.append(Verdict(name, False, problem, plugin.mode))
            elif enabled.digest != plugin.digest or current_digest(layout, plugin) != plugin.digest:
                out.append(Verdict(name, False, "files changed since the enable", plugin.mode))
            else:
                out.append(Verdict(name, True, None, plugin.mode))
    return out


# -- audit -------------------------------------------------------------------------------------


def audit(
    engine: Engine,
    action: str,
    target: str,
    *,
    actor_id: str | None = None,
    session_id: str | None = None,
    auth_time: datetime | None = None,
) -> None:
    """One row of the `audit` table. The actor is the host user that ran the command, unless a
    caller with a principal names its own (the HTTP routes: the session's user, its session and
    its `auth_time`, EF-USR-022 (3)) — the CLI keeps the uid, the route the account."""
    table = schema.tables["audit"]
    with write_transaction(engine) as connection:
        connection.execute(
            sa.insert(table).values(
                id=new_ulid(),
                actor_id=actor_id if actor_id is not None else f"cli:uid{os.getuid()}",
                action=action,
                target=target,
                session_id=session_id,
                auth_time=auth_time,
            )
        )
