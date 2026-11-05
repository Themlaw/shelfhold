"""The declarative file of `config apply -f shelfhold.toml` (EF-INT-024).

A free name, distinct from the `config.toml` read at start-up; same keys. Strict: an unknown key
is an error, a secret is the path of a file (`password_file`, `secret_file`), never a value.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from shelfhold.headless.errors import ConfigFileError
from shelfhold.headless.keys import MAINTENANCE_WINDOW

SUPPORTED_MAJOR: Final = 1
_TOP: Final = {"schema_version", "public_url", "source", "notify", "maintenance", "targets",
               "sources", "shelves"}  # fmt: skip
_TARGET: Final = {"kind", "name", "url", "user", "password_file", "accept_admin_risk", "path_map"}
_SOURCE: Final = {"kind", "url", "secret_file"}
_SHELF: Final = {"name", "max_age", "target", "subpath", "phrase"}


@dataclass(frozen=True)
class DesiredTarget:
    kind: str
    name: str
    url: str
    user: str
    password_file: Path | None
    accept_admin_risk: bool = False
    path_map: list[dict[str, str]] | None = None


@dataclass(frozen=True)
class DesiredSource:
    kind: str
    url: str
    secret_file: Path | None = None


@dataclass(frozen=True)
class DesiredShelf:
    name: str
    max_age: str
    target: str
    subpath: str = ""
    phrase: str | None = None


@dataclass(frozen=True)
class DesiredConfig:
    settings: dict[str, str] = field(default_factory=dict)
    notify: dict[str, str] = field(default_factory=dict)
    targets: tuple[DesiredTarget, ...] = ()
    sources: tuple[DesiredSource, ...] = ()
    shelves: tuple[DesiredShelf, ...] = ()


def _table(raw: Any, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigFileError(f"{what} must be a table")
    return raw


def _strict(raw: dict[str, Any], allowed: set[str], what: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigFileError(f"{what}: unknown key {unknown[0]!r}")


def _text(raw: dict[str, Any], key: str, what: str, *, required: bool = True) -> str | None:
    value = raw.get(key)
    if value is None:
        if required:
            raise ConfigFileError(f"{what}: {key!r} is required")
        return None
    if not isinstance(value, str) or not value:
        raise ConfigFileError(f"{what}: {key!r} must be a non-empty text")
    return value


def _path(raw: dict[str, Any], key: str, base: Path, what: str) -> Path | None:
    value = _text(raw, key, what, required=False)
    return None if value is None else (base / value)


def _path_map(raw: Any, what: str) -> list[dict[str, str]] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        raise ConfigFileError(f"{what}: path_map is a list of tables")
    return [{str(k): str(v) for k, v in entry.items()} for entry in raw]


def parse_desired(text: str, *, base_dir: Path) -> DesiredConfig:
    """Read the file; relative secret paths are taken from `base_dir`, the file's folder."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigFileError(f"not valid TOML: {error}") from None
    _strict(raw, _TOP, "file")
    version = str(raw.get("schema_version", "1.0"))
    if not version.split(".")[0].isdigit() or int(version.split(".")[0]) > SUPPORTED_MAJOR:
        raise ConfigFileError(f"schema_version {version} is newer than this release supports")
    settings: dict[str, str] = {}
    if "public_url" in raw:
        settings["public_url"] = str(_text(raw, "public_url", "file"))
    source = _table(raw.get("source", {}), "[source]")
    _strict(source, {"check_mode"}, "[source]")
    if "check_mode" in source:
        settings["source.check_mode"] = str(_text(source, "check_mode", "[source]"))
    maintenance = _table(raw.get("maintenance", {}), "[maintenance]")
    _strict(maintenance, {"window"}, "[maintenance]")
    if maintenance.get("window", MAINTENANCE_WINDOW) != MAINTENANCE_WINDOW:
        raise ConfigFileError("[maintenance] window is fixed in M1a")
    notify = _table(raw.get("notify", {}), "[notify]")
    _strict(notify, {"urls"}, "[notify]")
    urls = _table(notify.get("urls", {}), "[notify.urls]")
    channels = {str(name): str(_text(urls, name, "[notify.urls]")) for name in urls}
    targets = []
    for item in raw.get("targets", []):
        entry = _table(item, "[[targets]]")
        if "password" in entry or "secret" in entry:
            raise ConfigFileError("[[targets]]: a secret is the path of a file (password_file)")
        _strict(entry, _TARGET, "[[targets]]")
        flag = entry.get("accept_admin_risk", False)
        if not isinstance(flag, bool):
            raise ConfigFileError("[[targets]]: accept_admin_risk is true or false")
        targets.append(
            DesiredTarget(
                kind=str(_text(entry, "kind", "[[targets]]")),
                name=str(_text(entry, "name", "[[targets]]")),
                url=str(_text(entry, "url", "[[targets]]")),
                user=str(_text(entry, "user", "[[targets]]")),
                password_file=_path(entry, "password_file", base_dir, "[[targets]]"),
                accept_admin_risk=flag,
                path_map=_path_map(entry.get("path_map"), "[[targets]]"),
            )
        )
    sources = []
    for item in raw.get("sources", []):
        entry = _table(item, "[[sources]]")
        if "secret" in entry or "password" in entry:
            raise ConfigFileError("[[sources]]: a secret is the path of a file (secret_file)")
        _strict(entry, _SOURCE, "[[sources]]")
        sources.append(
            DesiredSource(
                kind=str(_text(entry, "kind", "[[sources]]")),
                url=str(_text(entry, "url", "[[sources]]")),
                secret_file=_path(entry, "secret_file", base_dir, "[[sources]]"),
            )
        )
    shelves = []
    for item in raw.get("shelves", []):
        entry = _table(item, "[[shelves]]")
        _strict(entry, _SHELF, "[[shelves]]")
        shelves.append(
            DesiredShelf(
                name=str(_text(entry, "name", "[[shelves]]")),
                max_age=str(_text(entry, "max_age", "[[shelves]]")),
                target=str(_text(entry, "target", "[[shelves]]")),
                subpath=str(entry.get("subpath", "")),
                phrase=_text(entry, "phrase", "[[shelves]]", required=False),
            )
        )
    return DesiredConfig(settings, channels, tuple(targets), tuple(sources), tuple(shelves))
