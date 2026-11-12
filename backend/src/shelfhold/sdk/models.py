# SPDX-License-Identifier: MIT
"""Common SDK types: plugin manifest, pagination, filters, authentication flows.

Extra-field policy: ``SdkModel`` ignores unknown fields (compatibility between N and N-1);
``StrictSdkModel`` forbids them, for the plans that the host executes.

Every type here round-trips through JSON (EF-SRC-001): bytes travel as base64, Decimal as a string,
flag sets as lists of names. Nothing in this module imports the AGPL core (ENF-LEG-010).
"""

from collections.abc import Callable
from datetime import datetime
from enum import Flag, StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    PlainValidator,
    WithJsonSchema,
    field_validator,
)

from shelfhold.sdk.lang import LanguageTag

API_VERSION = 1
"""Major of the plugin data contract. The host accepts API_VERSION and API_VERSION - 1 (shim)."""

SDK_VERSION = "1.3"
"""Major.minor of the SDK. 1.3 freezes the plugin boundary (M3 L34): the serialisable `FsOp` plan,
plugin event spaces, declared exits of plugin waiting states, `fetch_range` optional. 1.2 adds
MetadataProvider (EF-BIB-013, D16). A minor only adds optional
fields and new contracts, so a plugin written for 1.1 keeps loading (ADR-0024)."""


def sdk_supports(min_sdk: str, host: str = SDK_VERSION) -> bool:
    """True when a plugin asking for SDK `min_sdk` can run on `host` (same major, minor >=)."""
    try:
        need_major, need_minor = (int(part) for part in min_sdk.split("."))
        have_major, have_minor = (int(part) for part in host.split("."))
    except ValueError:
        return False
    return need_major == have_major and need_minor <= have_minor


class SdkModel(BaseModel):
    """Base of the SDK types read from a plugin or a manifest.

    Unknown fields are ignored, so that an addition to the contract stays compatible between api N
    and N-1 (ADR-0024: additions are optional fields). Bytes travel as base64.
    """

    model_config = ConfigDict(extra="ignore", ser_json_bytes="base64", val_json_bytes="base64")


class StrictSdkModel(SdkModel):
    """Types the host validates and executes (plans): an unknown field is an error, because
    silently dropping part of an operation would execute something other than what was described."""

    model_config = ConfigDict(extra="forbid")


def flag_parser[F: Flag](flag_cls: type[F]) -> Callable[[Any], F]:
    """Validator turning a list of member names (or an existing flag value) into a flag set."""

    def parse(value: Any) -> F:
        if isinstance(value, flag_cls):
            return value
        if isinstance(value, list | tuple | set | frozenset):
            result = flag_cls(0)
            for name in value:
                if not isinstance(name, str) or name not in flag_cls.__members__:
                    raise ValueError(f"unknown {flag_cls.__name__} member: {name!r}")
                result |= flag_cls[name]
            return result
        raise ValueError(f"{flag_cls.__name__} expects a list of member names")

    return parse


def flag_names[F: Flag](value: F) -> list[str]:
    """Sorted member names of a flag set, the JSON form of a capability set."""
    return sorted(m.name for m in type(value) if m in value and m.name)


FlagNames = Annotated[Any, WithJsonSchema({"type": "array", "items": {"type": "string"}})]


class PluginKind(StrEnum):
    SOURCE = "source"
    EXPORT_TARGET = "export-target"
    METADATA = "metadata"
    # reserved: contract sketched, not implemented in V1
    TRACKER = "tracker"
    IMPORTER = "importer"
    NOTIFIER = "notifier"
    PROCESSOR = "processor"


class DomainRule(SdkModel):
    pattern: str  # "example.org", "*.cdn.example.org" (suffix / wildcard)
    purpose: Literal["api", "pages", "covers", "auth", "other"]
    base_url_setting: str | None = None  # a setting may override the domain; logged (EF-SRC-003)
    # Limits the host to this path and what is under it (ENF-LEG-003): a host opened for one feed
    # is not opened for the rest of its site. Absolute, no dot segment, ends with "/".
    path_prefix: str | None = None

    @field_validator("path_prefix")
    @classmethod
    def _prefix_is_a_plain_directory(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.split("/")
        plain = (
            value.startswith("/")
            and value.endswith("/")
            and all(parts[1:-1])  # no empty segment ("//")
            and not any(p in (".", "..") for p in parts)
            and not any(c in value for c in "\\%?#:@ ")
        )
        if not plain:
            raise ValueError("path_prefix is an absolute directory path: /a/b/")
        return value


class Permissions(SdkModel):
    domains: list[DomainRule] = Field(default_factory=list)
    lan_targets: list[str] = Field(default_factory=list)  # explicit "host:port", never a boolean
    needs_browser: bool = False  # reserved (M2+), always behind the same SSRF guard
    needs_solver: bool = False  # reserved (M2+)


class RateLimit(SdkModel):
    requests_per_second: float
    concurrency: int = 1


WaitingExit = Literal["retry", "recheck", "reconfigure"]
WAITING_EXITS: tuple[str, ...] = ("retry", "recheck", "reconfigure")


class PluginWaitingState(SdkModel):
    """A state in which a plugin waits for a human. It names its way out (EF-INT-023): `retry`
    (try the same step again), `recheck` (look at the upstream again), `reconfigure` (change a
    setting). Its event is `plugin.<key>.<name>`."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    exit: WaitingExit


class PluginManifest(SdkModel):
    key: str  # stable, never derived from the display name
    kind: PluginKind
    version: str  # plugin semver
    api: int  # major of the contract: N or N-1 (shim)
    name: str
    permissions: Permissions = Field(default_factory=Permissions)
    settings_schema: dict[str, Any] = Field(default_factory=dict)  # JSON Schema -> generated UI
    content_warning: Literal["safe", "mixed", "nsfw"] = "safe"
    terms_url: str | None = None  # terms of use (ENF-LEG-003)
    respects_robots: bool = True
    attribution: str | None = None  # attribution text shown in the UI (ENF-LEG-005)
    homepage: str | None = None
    waiting_states: list[PluginWaitingState] = Field(default_factory=list)  # SDK 1.3

    @field_validator("waiting_states")
    @classmethod
    def _states_are_distinct(cls, value: list[PluginWaitingState]) -> list[PluginWaitingState]:
        names = [s.name for s in value]
        if len(set(names)) != len(names):
            raise ValueError("two waiting states with one name")
        return value


class Cursor(SdkModel):
    token: str  # opaque to the host, never an imposed page number


class Page[T](SdkModel):
    items: list[T]
    next: Cursor | None = None


class FilterSpec(SdkModel):
    key: str
    kind: Literal["select", "text", "checkbox", "tristate", "group", "sort"]
    label: str
    options: list[tuple[str, str]] = Field(default_factory=list)
    default: Any = None
    children: list["FilterSpec"] = Field(default_factory=list)


# --- authentication (EF-SRC-023). V1 implements Basic and ApiKey; the others are reserved. ---
class AuthBasic(SdkModel):
    kind: Literal["basic"] = "basic"


class AuthForm(SdkModel):
    kind: Literal["form"] = "form"
    fields: list[str]


class AuthApiKey(SdkModel):
    kind: Literal["api_key"] = "api_key"
    location: Literal["header", "query", "path"]
    name: str
    # The template the adapter declares for the references it hands back: the host rewrites the
    # key segment of a reference to this placeholder instead of refusing it (EF-SRC-035).
    ref_template: Literal["{secret}"] | None = None


class AuthCookieImport(SdkModel):
    kind: Literal["cookie_import"] = "cookie_import"
    domains: list[str]


class AuthDeviceCode(SdkModel):
    kind: Literal["oauth2_device"] = "oauth2_device"
    device_url: str
    token_url: str
    scopes: list[str]


class AuthInteractive(SdkModel):
    kind: Literal["interactive"] = "interactive"
    reason: Literal["captcha", "consent"]


AuthFlow = Annotated[
    AuthBasic | AuthForm | AuthApiKey | AuthCookieImport | AuthDeviceCode | AuthInteractive,
    Field(discriminator="kind"),
]


class SessionState(SdkModel):
    expires_at: datetime | None = None
    opaque: dict[str, Any] = Field(default_factory=dict)  # stored encrypted by the host, per user


__all__ = [
    "API_VERSION",
    "SDK_VERSION",
    "WAITING_EXITS",
    "AuthApiKey",
    "AuthBasic",
    "AuthCookieImport",
    "AuthDeviceCode",
    "AuthFlow",
    "AuthForm",
    "AuthInteractive",
    "Cursor",
    "DomainRule",
    "FilterSpec",
    "FlagNames",
    "LanguageTag",
    "Page",
    "Permissions",
    "PlainSerializer",
    "PlainValidator",
    "PluginKind",
    "PluginManifest",
    "PluginWaitingState",
    "RateLimit",
    "SdkModel",
    "SessionState",
    "StrictSdkModel",
    "WaitingExit",
    "flag_names",
    "flag_parser",
    "sdk_supports",
]
