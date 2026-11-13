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

from shelfhold.infrastructure.http_headers import require_web_url
from shelfhold.jobs.window import parse_clock, parse_zone

ENV_PREFIX = "SHELFHOLD_"
FILE_SUFFIX = "_FILE"
CONFIG_FILE_NAME = "config.toml"
CONFIG_SCHEMA_VERSION = "1.0"  # "major.minor", ADR-0039
SUPPORTED_CONFIG_MAJOR = 1

# Fields that locate the other layers: they can only come from the environment.
GROUP_PREFIX = "group:"
ENV_ONLY_FIELDS = frozenset({"data_dir", "secret_key_file", "backup_passphrase_file"})

Source = Literal["env", "toml", "db", "default"]
DbLoader = Callable[[Path], Mapping[str, str]]


class ConfigError(Exception):
    """The configuration cannot be resolved; the process must not start."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_dir: Path = Path("/data")
    store_dir: Path = Path("/store")
    library_dir: Path = Path("/library")  # the physical roots live under it (EF-EXP-059)
    # EF-SRC-012: the folder the admin drops their own CBZ, EPUB and PDF files in. Off while unset;
    # it may not hold, or sit inside, /data, /store or /library: Shelfhold would take its own
    # output back in.
    import_dir: Path | None = None
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
    # EF-BIB-024: a new version of a chapter that counts less than this share of the pages of the
    # stored one is quarantined and the stored one stays exported. 0.5 by default.
    revision_min_page_ratio: float = Field(default=0.5, ge=0.05, le=1.0)
    # EF-SRC-037: a binding that fell back to the next rank because its upstream source was not
    # loaded goes back to its own rank when the source is loaded again. Off: it stays where it fell.
    follow_fallback_return: bool = True
    # EF-SRC-038: the adaptive check cadence of a piloted follow. The wait between two looks at a
    # series follows its observed rhythm, never under `follow_check_min_s` (1 h) nor over
    # `follow_check_max_s` (7 days); a finished or paused series is looked at once per
    # `follow_check_finished_s` (30 days) at most.
    follow_check_min_s: int = Field(default=3_600, ge=60)
    follow_check_max_s: int = Field(default=7 * 86_400, ge=60)
    follow_check_finished_s: int = Field(default=30 * 86_400, ge=3_600)
    # Q50: a non-empty numbering segment stays in `numbering_conflict`, unexported, until the
    # naming golden (one Kavita series per segment) is confirmed by the nightly against a real
    # Kavita (L40). Leave it false; the lot that records the confirmation flips the default.
    naming_segment_policy_confirmed: bool = False
    # D15: the proxies whose `X-Forwarded-For` is read (the real address of a caller, for the rate
    # limits) and, when forward-auth is switched on, whose identity headers are believed. A request
    # whose socket peer is outside these networks is only ever what its peer is.
    http_trusted_proxies: tuple[str, ...] = ()
    # P5, EF-USR-002: forward-auth is off by default. Switched on, it needs `http_trusted_proxies`
    # and, when `auth_forward_secret` is set, the same value in `X-Shelfhold-Proxy-Secret`
    # (constant-time comparison). Proxy identity headers (Remote-User, Tailscale-User-*...) are
    # removed from every other request: never believed, never an error of their own.
    auth_forward_enabled: bool = False
    auth_forward_secret: SecretStr | None = None
    # ENF-SEC-008: requests per minute and per real address on the public routes (login page,
    # sign-in, anything an anonymous caller may reach). Failures of sign-in have their own limits.
    http_public_rate_per_minute: int = Field(default=120, ge=10, le=100_000)
    # ENF-SEC-008: the routes that keep their own credentials (`/opds`, `/kosync`, `/kobo`: a device
    # token or Basic) are limited per credential, not per address, and not by the figure above.
    http_device_rate_per_minute: int = Field(default=600, ge=10, le=100_000)
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
    # EF-INT-022 (ADR-0025): the WakeHook, optional. `wake_url` is the API of Sablier (a declared
    # LAN address of the owner, like Suwayomi and Kavita); `wake_suwayomi` and `wake_kavita` name
    # the containers to start for each tool, or `group:<name>` alone. Without them nothing is woken.
    # `wake_token` is sent as a bearer token to a Sablier behind an authenticating proxy; it is
    # never logged. The wake delay is its own, apart from every request delay.
    wake_url: str | None = None
    wake_token: SecretStr | None = None
    wake_suwayomi: tuple[str, ...] = ()
    wake_kavita: tuple[str, ...] = ()
    wake_timeout_s: int = Field(default=60, ge=5, le=600)
    wake_session_s: int = Field(default=300, ge=30, le=3600)
    # ENF-OPS-014 (settable from M1b): the window in which the jobs that touch `/store` or
    # `/library` run, so that the hard disks can sleep the rest of the day. `HH:MM` in the
    # instance's time zone; an end before the start crosses midnight ("23:00" to "02:00").
    # `timezone` is also the zone of every cron expression of the schedule.
    maintenance_window_start: str = "03:00"
    maintenance_window_end: str = "07:00"
    timezone: str = "Europe/Paris"
    # ENF-OPS-009: how long the technical history is kept. Events and finished jobs 90 days, the
    # audit one year (never shortened below 90 days: it is the trace of who did what). The
    # recycle bins of the library keep 30 days, a floor that fsops enforces whatever is set here.
    retention_events_days: int = Field(default=90, ge=7, le=3650)
    retention_audit_days: int = Field(default=365, ge=90, le=3650)
    # M3 L12: how many event streams (SSE) one profile may hold open at once, across its sessions
    # and keys. Each stream holds a subscription and a read on every event; beyond the cap a new
    # one is refused (429) until another closes.
    stream_max_per_profile: int = Field(default=4, ge=1, le=64)

    # EF-INT-015 (D2): the integrated backup. `backup_dir` is a mounted folder outside `/data` and
    # `/store`; `backup_passphrase_file` names the file that holds the passphrase (a path, never
    # the passphrase; environment only: SHELFHOLD_BACKUP_PASSPHRASE_FILE). 14 daily backups are
    # kept; `backup.stale` follows a last good backup older than `backup_stale_hours`.
    backup_dir: Path | None = None
    backup_passphrase_file: Path | None = None
    backup_retention: int = Field(default=14, ge=1, le=30)
    backup_stale_hours: int = Field(default=36, ge=25, le=24 * 60)

    # ENF-LEG-007: the HTTP cache of the Fetcher, and the sources (plugin ids) it is off for.
    fetcher_cache: bool = True
    fetcher_cache_off_sources: tuple[str, ...] = ()

    # ENF-LEG-005 (D7): the instance's own contact (an e-mail address or an https URL), added to
    # the agent of the metadata providers and opening their higher quota (3 requests per second
    # at OpenLibrary instead of 1). Never a default, never an address of the maintainer; never
    # logged: it is an address of the owner.
    metadata_contact: str | None = None

    @field_validator("source_url")
    @classmethod
    def _source_url_is_web(cls, value: str | None) -> str | None:
        return None if value is None else require_web_url(value)

    @field_validator("metadata_contact")
    @classmethod
    def _contact_fits_an_agent(cls, value: str | None) -> str | None:
        if value is not None:
            from shelfhold.infrastructure.fetcher.client import identifiable_user_agent

            identifiable_user_agent(value)  # ValueError: not an address nor an https URL
        return value

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

    @field_validator("http_trusted_proxies")
    @classmethod
    def _cidrs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(str(ipaddress.ip_network(cidr, strict=False)) for cidr in value)

    @field_validator(
        "plugin_repos", "active_sources", "http_trusted_proxies", "wake_suwayomi",
        "wake_kavita", mode="before",
    )  # fmt: skip
    @classmethod
    def _split_list(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("maintenance_window_start", "maintenance_window_end")
    @classmethod
    def _window_clock(cls, value: str) -> str:
        parse_clock(value)
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        parse_zone(value)
        return value

    @model_validator(mode="after")
    def _forward_auth_needs_a_network(self) -> Self:
        networks = [ipaddress.ip_network(cidr) for cidr in self.http_trusted_proxies]
        if any(network.prefixlen == 0 for network in networks):
            # every address is "a proxy": any caller names herself, or is any visitor's address
            raise ValueError("http_trusted_proxies must not hold every address (/0)")
        if self.auth_forward_enabled and not networks:
            raise ValueError("forward-auth needs http_trusted_proxies (the network of the proxy)")
        wide = any(n.prefixlen < n.max_prefixlen for n in networks)
        secret = self.auth_forward_secret
        if self.auth_forward_enabled and wide and (secret is None or not secret.get_secret_value()):
            # a network is many hosts, and one of them is not the proxy: only the secret tells
            raise ValueError(
                "forward-auth on a network wider than one host needs auth_forward_secret"
            )
        return self

    @model_validator(mode="after")
    def _window_not_empty(self) -> Self:
        if parse_clock(self.maintenance_window_start) == parse_clock(self.maintenance_window_end):
            raise ValueError("the maintenance window must not be empty or last all day")
        return self

    @field_validator("wake_suwayomi", "wake_kavita")
    @classmethod
    def _wake_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        groups = [v for v in value if v.startswith(GROUP_PREFIX)]
        if groups and (len(value) > 1 or not value[0][len(GROUP_PREFIX) :]):
            raise ValueError("a Sablier group is given alone, as group:<name>")
        return value

    def wake_targets(self) -> dict[str, tuple[tuple[str, ...], str | None]]:
        """Label -> (container names, group) of every tool a WakeHook is attached to."""
        found: dict[str, tuple[tuple[str, ...], str | None]] = {}
        for label, entries in (("suwayomi", self.wake_suwayomi), ("kavita", self.wake_kavita)):
            if not entries:
                continue
            if entries[0].startswith(GROUP_PREFIX):
                found[label] = ((), entries[0][len(GROUP_PREFIX) :])
            else:
                found[label] = (entries, None)
        return found

    @property
    def wake_configured(self) -> bool:
        """A WakeHook is configured: an address and at least one tool behind it (D8)."""
        return bool(self.wake_url) and bool(self.wake_targets())

    @property
    def database_path(self) -> Path:
        return self.data_dir / "shelfhold.db"

    @model_validator(mode="after")
    def _check_min_not_above_max(self) -> Self:
        if self.follow_check_min_s > self.follow_check_max_s:
            raise ValueError("follow_check_min_s (check_min) must not exceed follow_check_max_s")
        return self

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

    @model_validator(mode="after")
    def _import_dir_is_apart(self) -> Self:
        folder = self.import_dir
        if folder is None:
            return self
        for name in ("data_dir", "store_dir", "library_dir"):
            other = getattr(self, name)
            if folder == other or folder in other.parents or other in folder.parents:
                raise ValueError(f"import_dir must be distinct from {name} and not nested in it")
            try:  # a bind mount of a root shows another path but the same device and inode
                if _identity(folder) == _identity(other):
                    raise ValueError(f"import_dir is the same directory as {name}")
            except OSError:
                continue
        return self


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


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
