"""Configuration with the precedence env > config.toml > database (ENF-OPS-004, ENF-OPS-012).

Pure module: no database or framework import. The database layer is injected
as a loader so that this module stays testable with a plain mapping.
"""

import ipaddress
import re
import stat
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

ENV_PREFIX = "SHELFHOLD_"
FILE_SUFFIX = "_FILE"
CONFIG_FILE_NAME = "config.toml"
CONFIG_SCHEMA_VERSION = "1.0"  # "major.minor", ADR-0039
SUPPORTED_CONFIG_MAJOR = 1

# Fields that locate the other layers: they can only come from the environment.
ENV_ONLY_FIELDS = frozenset({"data_dir", "secret_key_file"})

Source = Literal["env", "toml", "db", "default"]
DbLoader = Callable[[Path], Mapping[str, str]]


class ConfigError(Exception):
    """The configuration cannot be resolved; the process must not start."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_dir: Path = Path("/data")
    store_dir: Path = Path("/store")
    library_dir: Path = Path("/library")  # the physical roots live under it (EF-EXP-059)
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    # EF-EXP-006: the umask of the files and folders Shelfhold creates under /library (octal text,
    # "0002" to let a Kavita in the same group write, "0022" by default: readable by any uid, so
    # a Kavita under another uid can read them). The process itself stays at 077 for /data, and
    # the secrets are 0600 by an explicit mode, whatever this says.
    umask: int = Field(default=0o022, ge=0, le=0o777)
    public_url: str | None = None
    api_token: SecretStr | None = None
    # Where the keyring lives when it must not be in /data (ENF-SEC-005); a path, not a secret.
    secret_key_file: Path | None = None
    frontend_dir: Path | None = None  # built SPA to serve; None = the packaged build, if any
    # ENF-LEG-001: a fresh install has no plugin repository and no active source.
    plugin_repos: tuple[str, ...] = ()
    active_sources: tuple[str, ...] = ()
    # ENF-LEG-012: where the running code is offered (login page, /api/v1/about); None = the
    # repository at the tag of this version. Point it at your fork if you modified the code.
    source_url: str | None = None
    # ENF-SEC-002: the most a connected service may send as one comic archive (a CBZ). Streamed to
    # the staging file and checked against this bound while it arrives; 512 MiB by default.
    received_cbz_max_bytes: int = Field(default=512 * 1024 * 1024, ge=1024 * 1024)
    # P1/P2/P3: the hardened profile (docs/admin/durcir.md) shortens admin sessions and asks for
    # a fresh password on every reserved action; never required. The invisible protections
    # (argon2id, CSRF, cookies, CSP, attempt limiting) have no setting at all.
    auth_hardened: bool = False
    fresh_auth_s: int = Field(default=900, ge=60, le=86_400)
    # Q50: a non-empty numbering segment stays in `numbering_conflict`, unexported, until the
    # naming golden (one Kavita series per segment) is confirmed by the nightly against a real
    # Kavita (L40). Leave it false; the lot that records the confirmation flips the default.
    naming_segment_policy_confirmed: bool = False
    # P5: forward-auth is off while this is empty. Proxy identity headers (Remote-User,
    # Tailscale-User-*...) are removed from every request whose peer is outside these networks.
    forward_auth_trusted_cidrs: tuple[str, ...] = ()
    # EF-USR-024 (GAP-13-07): the local CLI socket refuses a peer outside the PID namespace of the
    # container (the host, another container that mounts /data) unless this is set. The uid of
    # Shelfhold on the host is then as powerful as an admin.
    cli_allow_host: bool = False
    # ENF-OPS-010: `/data` on NFS or CIFS is refused at start-up (statfs) unless this is set.
    allow_network_data_dir: bool = False
    # ENF-OPS-006: the disk guard's marks on `/data`, `/store` and the library. Writes pause under
    # max(pause_percent, pause_gib) free and resume at max(resume_percent, resume_gib).
    disk_pause_percent: float = Field(default=5.0, ge=0, le=50)
    disk_pause_gib: float = Field(default=10.0, ge=0)
    disk_resume_percent: float = Field(default=8.0, ge=0, le=60)
    disk_resume_gib: float = Field(default=15.0, ge=0)

    @field_validator("umask", mode="before")
    @classmethod
    def _octal_umask(cls, value: object) -> object:
        """`SHELFHOLD_UMASK=0027` is octal text; an integer (a TOML `0o027`) is taken as is."""
        if isinstance(value, str):
            if not re.fullmatch(r"0?[0-7]{1,3}", value.strip()):
                raise ValueError("umask must be octal text such as 0022")
            value = int(value.strip(), 8)
        return value

    @field_validator("umask")
    @classmethod
    def _safe_umask(cls, value: int) -> int:
        if value & 0o700:
            raise ValueError("umask must not mask the owner's bits: files would be unusable")
        if not value & 0o002:
            raise ValueError("umask must mask 'others write' (0o002 set), e.g. 0022 or 0002")
        return value

    @field_validator("forward_auth_trusted_cidrs")
    @classmethod
    def _cidrs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(str(ipaddress.ip_network(cidr, strict=False)) for cidr in value)

    @field_validator("plugin_repos", "active_sources", "forward_auth_trusted_cidrs", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @property
    def database_path(self) -> Path:
        return self.data_dir / "shelfhold.db"

    @model_validator(mode="after")
    def _resume_above_pause(self) -> Self:
        if (
            self.disk_resume_percent <= self.disk_pause_percent
            or self.disk_resume_gib <= self.disk_pause_gib
        ):
            raise ValueError("the disk resume marks must be strictly above the pause marks")
        return self

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
    world_readable_secret_files: tuple[str, ...] = ()


def _env_layer(env: Mapping[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    values: dict[str, str] = {}
    exposed: list[str] = []
    for name in Settings.model_fields:
        key = f"{ENV_PREFIX}{name.upper()}"
        direct = env.get(key) or None
        file_path = env.get(key + FILE_SUFFIX) or None
        if direct is not None and file_path is not None:
            raise ConfigError(f"{key} and {key}{FILE_SUFFIX} are both set")
        if file_path is not None:
            try:
                secret_path = Path(file_path)
                direct = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
                if secret_path.stat().st_mode & stat.S_IROTH:
                    exposed.append(file_path)
            except (OSError, UnicodeDecodeError) as exc:
                raise ConfigError(f"cannot read {key}{FILE_SUFFIX}: {exc}") from exc
        if direct is not None:
            values[name] = direct
    return values, tuple(exposed)


def _toml_layer(path: Path) -> tuple[dict[str, Any], str]:
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
    return {k: v for k, v in raw.items() if k in Settings.model_fields}, version


def load_settings(env: Mapping[str, str], db_loader: DbLoader | None = None) -> ResolvedSettings:
    """Resolve every field from env, then config.toml, then the database, then defaults."""
    env_values, exposed = _env_layer(env)
    data_dir = Path(env_values.get("data_dir", str(Settings.model_fields["data_dir"].default)))
    toml_values, schema_version = _toml_layer(data_dir / CONFIG_FILE_NAME)
    db_values: dict[str, Any] = dict(db_loader(data_dir)) if db_loader else {}

    merged: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    for name in Settings.model_fields:
        layers: list[tuple[Source, Mapping[str, Any]]] = [("env", env_values)]
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
    return ResolvedSettings(
        values=values,
        fields=fields,
        schema_version=schema_version,
        world_readable_secret_files=exposed,
    )
