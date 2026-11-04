"""Configuration with the precedence env > config.toml > database (ENF-OPS-004, ENF-OPS-012).

Pure module: no database or framework import. The database layer is injected
as a loader so that this module stays testable with a plain mapping.
"""

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

ENV_PREFIX = "SHELFHOLD_"
FILE_SUFFIX = "_FILE"
CONFIG_FILE_NAME = "config.toml"
CONFIG_SCHEMA_VERSION = "1.0"  # "major.minor", ADR-0039
SUPPORTED_CONFIG_MAJOR = 1

# Fields that locate the other layers: they can only come from the environment.
ENV_ONLY_FIELDS = frozenset({"data_dir"})

Source = Literal["env", "toml", "db", "default"]
DbLoader = Callable[[Path], Mapping[str, str]]


class ConfigError(Exception):
    """The configuration cannot be resolved; the process must not start."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_dir: Path = Path("/data")
    store_dir: Path = Path("/store")
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    public_url: str | None = None
    kavita_api_key: SecretStr | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "shelfhold.db"

    @model_validator(mode="after")
    def _volumes_are_distinct(self) -> Self:
        data, store = self.data_dir, self.store_dir
        if data == store or data in store.parents or store in data.parents:
            raise ValueError("data_dir and store_dir must be distinct and not nested")
        return self


@dataclass(frozen=True)
class FieldState:
    name: str
    source: Source

    @property
    def readonly(self) -> bool:
        """A field defined by the environment cannot be edited from the UI."""
        return self.source == "env"


@dataclass(frozen=True)
class ResolvedSettings:
    values: Settings
    fields: Mapping[str, FieldState]
    schema_version: str


def _env_layer(env: Mapping[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in Settings.model_fields:
        key = f"{ENV_PREFIX}{name.upper()}"
        direct = env.get(key) or None
        file_path = env.get(key + FILE_SUFFIX) or None
        if direct is not None and file_path is not None:
            raise ConfigError(f"{key} and {key}{FILE_SUFFIX} are both set")
        if file_path is not None:
            try:
                direct = Path(file_path).read_text(encoding="utf-8").rstrip("\r\n")
            except OSError as exc:
                raise ConfigError(f"cannot read {key}{FILE_SUFFIX}: {exc}") from exc
        if direct is not None:
            values[name] = direct
    return values


def _toml_layer(path: Path) -> tuple[dict[str, str], str]:
    if not path.is_file():
        return {}, CONFIG_SCHEMA_VERSION
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    version = str(raw.get("schema_version", CONFIG_SCHEMA_VERSION))
    if not version.split(".")[0].isdigit() or int(version.split(".")[0]) > SUPPORTED_CONFIG_MAJOR:
        raise ConfigError(f"{path} has schema_version {version}, newer than this release supports")
    # Unknown keys are ignored here and left untouched on disk.
    return {k: str(v) for k, v in raw.items() if k in Settings.model_fields}, version


def load_settings(env: Mapping[str, str], db_loader: DbLoader | None = None) -> ResolvedSettings:
    """Resolve every field from env, then config.toml, then the database, then defaults."""
    env_values = _env_layer(env)
    data_dir = Path(env_values.get("data_dir", str(Settings.model_fields["data_dir"].default)))
    toml_values, schema_version = _toml_layer(data_dir / CONFIG_FILE_NAME)
    db_values = dict(db_loader(data_dir)) if db_loader else {}

    merged: dict[str, str] = {}
    sources: dict[str, Source] = {}
    for name in Settings.model_fields:
        layers: list[tuple[Source, Mapping[str, str]]] = [("env", env_values)]
        if name not in ENV_ONLY_FIELDS:
            layers += [("toml", toml_values), ("db", db_values)]
        for source, layer in layers:
            if name in layer:
                merged[name] = layer[name]
                sources[name] = source
                break
        else:
            sources[name] = "default"

    try:
        values = Settings.model_validate(merged)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(detail) from exc
    fields = {name: FieldState(name, source) for name, source in sources.items()}
    return ResolvedSettings(values=values, fields=fields, schema_version=schema_version)
