"""Installing third-party plugins (ENF-SEC-009, EF-SRC-019).

A plugin is a local wheel file with its sha256. It is installed with
``--only-binary :all: --require-hashes --no-deps`` (nothing is resolved, built or downloaded: the
command also says ``--no-index``) into a ``site-packages`` of its own under ``<data>/plugins``,
which the host appends to ``sys.path`` after its own entries (``registry``). Before any file is
written the wheel is read as a zip and refused when:

- its distribution name, after PEP 503 normalisation, is the host's or one of its dependencies'
  (``shelfhold``, ``shelfhold-sdk``, ``httpx``...) or an installed plugin's;
- one of its top-level modules is a standard-library module, a module the host can import or a
  name already imported (a plugin can neither mask nor extend the host);
- it carries a ``.pth`` file, a ``.data`` directory, a path leaving the archive, or a
  ``*.dist-info`` that is not its own (a fake ``shelfhold-*.dist-info``).

The recorded state keeps, per plugin, the wheel hash and a digest of the installed files. The typed
confirmation (``enable``) is bound to that digest: reinstalling or editing the files voids it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from importlib.machinery import PathFinder
from pathlib import Path, PurePosixPath
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import metadata as schema
from shelfhold.infrastructure.secrets.atomic import write_private
from shelfhold.plugin_host.registry import normalise_name as normalise_name

ENV_ALLOW = "SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS"
CONFIRMATION = "mêmes droits que Shelfhold"
RESERVED_DISTRIBUTIONS = frozenset({"shelfhold", "shelfhold-sdk", "httpx", "httpcore"})
RESERVED_MODULES = frozenset({"shelfhold", "httpx", "httpcore"})  # whatever is installed here
PIP_FLAGS = ("--only-binary", ":all:", "--require-hashes", "--no-deps", "--no-index")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIST_INFO = re.compile(r"^(?P<name>.+)-(?P<version>[^-]+)\.dist-info$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


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


@dataclass(frozen=True)
class Confirmation:
    digest: str  # of the installed files at the time of the typed confirmation
    at: str


@dataclass
class PluginFile:
    installed: dict[str, Installed]
    confirmed: dict[str, Confirmation]


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
            )
            for n, v in raw.get("installed", {}).items()
        }
        confirmed = {
            n: Confirmation(str(v["digest"]), str(v["at"]))
            for n, v in raw.get("confirmed", {}).items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return PluginFile({}, {})  # unreadable or absent: nothing is installed, nothing confirmed
    return PluginFile(installed, confirmed)


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
            }
            for n, i in sorted(state.installed.items())
        },
        "confirmed": {
            n: {"digest": c.digest, "at": c.at} for n, c in sorted(state.confirmed.items())
        },
    }
    write_private(layout.state, json.dumps(payload, indent=1).encode("utf-8"), overwrite=True)


def tree_digest(site: Path, entries: Sequence[str]) -> str:
    """Digest of the files of a plugin (bytecode caches left out: importing writes them)."""
    h = hashlib.sha256()
    for entry in sorted(entries):
        base = site / entry
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for path in files:
            rel = path.relative_to(site).as_posix()
            if "__pycache__" in rel or rel.endswith(".pyc"):
                continue
            h.update(rel.encode() + b"\0")
            h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


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


def _top_level(path: str) -> str:
    first = PurePosixPath(path).parts[0]
    return first[:-3] if first.endswith(".py") else first


def inspect_wheel(wheel: Path) -> WheelInfo:
    if wheel.suffix != ".whl":
        raise PluginInstallError("bad_wheel", "not a .whl file")
    try:
        archive = zipfile.ZipFile(wheel)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PluginInstallError("bad_wheel", str(exc)) from exc
    with archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        for n in names:
            parts = PurePosixPath(n).parts
            if n.startswith("/") or ".." in parts or "\\" in n or not parts:
                raise PluginInstallError("bad_wheel", f"unsafe path {n!r}")
            if n.lower().endswith(".pth"):
                raise PluginInstallError("bad_wheel", f".pth file {n!r} (never executed)")
            if parts[0].endswith(".data"):
                raise PluginInstallError("bad_wheel", f"{parts[0]!r}: data directories refused")
        infos = {
            PurePosixPath(n).parts[0]
            for n in names
            if PurePosixPath(n).parts[0].endswith(".dist-info")
        }
        if len(infos) != 1:
            raise PluginInstallError("bad_wheel", f"{len(infos)} dist-info directories")
        dist_info = infos.pop()
        match = _DIST_INFO.match(dist_info)
        if match is None:
            raise PluginInstallError("bad_wheel", f"bad dist-info name {dist_info!r}")
        try:
            meta = archive.read(f"{dist_info}/METADATA").decode("utf-8", "replace")
        except KeyError as exc:
            raise PluginInstallError("bad_wheel", "no METADATA") from exc
    declared = re.search(r"^Name:\s*(\S+)\s*$", meta, re.MULTILINE)
    if declared is None:
        raise PluginInstallError("bad_wheel", "METADATA declares no Name")
    name = normalise_name(declared.group(1))
    if normalise_name(match.group("name")) != name:
        raise PluginInstallError("bad_wheel", "dist-info name differs from METADATA Name")
    tops = sorted(
        {_top_level(n) for n in names if not PurePosixPath(n).parts[0].endswith(".dist-info")}
    )
    if not tops:
        raise PluginInstallError("bad_wheel", "no module")
    return WheelInfo(name, match.group("version"), tuple(tops), dist_info)


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
    uv = shutil.which("uv")
    if uv is None:
        raise PluginInstallError("no_installer", "neither pip nor uv is available")
    return [uv, "pip", "install", "--python", sys.executable]


def install_command(prefix: Sequence[str], target: Path, requirements: Path) -> list[str]:
    return [*prefix, "--target", str(target), *PIP_FLAGS, "-r", str(requirements)]


def requirement_line(name: str, wheel: Path, sha256: str) -> str:
    return f"{name} @ {wheel.resolve().as_uri()} --hash=sha256:{sha256}\n"


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, local file only
        list(argv), capture_output=True, text=True, check=False, timeout=300
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
        done = runner(argv)
        if done.returncode != 0:
            raise PluginInstallError("install_failed", (done.stderr or done.stdout).strip()[-400:])
        _check_staged(target, info)
        layout.site.mkdir(parents=True, exist_ok=True, mode=0o700)
        for entry in on_disk(target, info.top_levels, info.dist_info):
            os.replace(target / entry, layout.site / entry)
        digest = tree_digest(layout.site, on_disk(layout.site, info.top_levels, info.dist_info))
        installed = Installed(
            info.name, info.version, expected, info.top_levels, info.dist_info, digest
        )
        state.installed[info.name] = installed
        state.confirmed.pop(info.name, None)
        write_state(layout, state)
        return installed
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _check_staged(target: Path, info: WheelInfo) -> None:
    """What the installer put down is what the wheel declared, nothing else (no `bin/`, no extra
    dist-info, no `.pth`)."""
    allowed = {
        info.dist_info,
        *info.top_levels,
        *(f"{t}.py" for t in info.top_levels),
        "__pycache__",
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
    for entry in on_disk(layout.site, plugin.top_levels, plugin.dist_info):
        path = layout.site / entry
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    del state.installed[key]
    state.confirmed.pop(key, None)
    write_state(layout, state)
    return plugin


# -- activation --------------------------------------------------------------------------------


def env_allows(environ: dict[str, str] | os._Environ[str]) -> bool:
    return environ.get(ENV_ALLOW) == "1"


def confirm(layout: Layout, name: str, typed: str) -> Confirmation:
    """Record the typed confirmation for the files as they are now."""
    state = read_state(layout)
    key = normalise_name(name)
    plugin = state.installed.get(key)
    if plugin is None:
        raise PluginInstallError("unknown", f"{name!r} is not installed")
    if typed.strip() != CONFIRMATION:
        raise PluginInstallError("confirmation_mismatch")
    digest = current_digest(layout, plugin)
    if digest != plugin.digest:
        raise PluginInstallError("modified", "installed files differ from the recorded install")
    confirmation = Confirmation(digest, datetime.now(UTC).isoformat())
    state.confirmed[key] = confirmation
    write_state(layout, state)
    return confirmation


def unconfirm(layout: Layout, name: str) -> None:
    state = read_state(layout)
    state.confirmed.pop(normalise_name(name), None)
    write_state(layout, state)


@dataclass(frozen=True)
class Verdict:
    name: str
    granted: bool
    reason: str | None = None


def verdicts(layout: Layout, environ: dict[str, str] | os._Environ[str]) -> list[Verdict]:
    """For each installed plugin: granted only with the variable, a confirmation and unchanged
    files. Each condition is checked again at every start; none is remembered on its own."""
    state = read_state(layout)
    out = []
    for name, plugin in sorted(state.installed.items()):
        confirmation = state.confirmed.get(name)
        if not env_allows(environ):
            out.append(Verdict(name, False, f"{ENV_ALLOW}=1 is not set"))
        elif confirmation is None:
            out.append(Verdict(name, False, "no typed confirmation (shelfhold plugin enable)"))
        elif (
            confirmation.digest != plugin.digest or current_digest(layout, plugin) != plugin.digest
        ):
            out.append(Verdict(name, False, "files changed since the confirmation"))
        else:
            out.append(Verdict(name, True))
    return out


# -- audit -------------------------------------------------------------------------------------


def audit(engine: Engine, action: str, target: str) -> None:
    """One row of the `audit` table; the actor is the host user that ran the command."""
    table = schema.tables["audit"]
    with engine.begin() as connection:
        connection.execute(
            sa.insert(table).values(
                id=new_ulid(), actor_id=f"cli:uid{os.getuid()}", action=action, target=target
            )
        )
