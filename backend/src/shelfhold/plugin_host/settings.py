"""Per-source settings: `settings_schema`, plain values, sealed secrets and cookies (EF-SRC-013).

A source describes its settings with a small JSON Schema (`settings_schema`, an object of flat
properties). The host validates the schema when the plugin loads and every value the admin gives.
A property marked `writeOnly: true` or `format: password` is a secret.

- Plain values live in `source_config.settings` (nothing secret there).
- Each secret and the cookie jar are sealed one by one in `source_secret` with the ENF-SEC-005
  envelope. The row id `<plugin>/<kind>/<field>` is the row coordinate of the associated data, so
  a sealed value copied to another source or another field does not open.
- Nothing reads a secret back: a view says "set" or "not set" and gives a short fingerprint, an
  HMAC keyed from the keyring (no offline guessing from the database alone).
- Every value opened is registered with `register_secret_value` at once, in every form it is used
  in (the value, `name=value`, the `Cookie` header), so the log scrubber, the write guard, the
  HTTP cache and the diagnostic bundle know it before anything can quote it.

Proxy, anti-bot solver, TLS fingerprint impersonation and headless browser are reserved for the
first native source that needs them, behind the same SSRF guard: a schema that declares one is
refused. The proxy, when it comes, routes the household's network; it is never a way around a
block (ENF-LEG-006, docs/admin/sources.md).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.schema import acquisition, identity
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.vault import SecretVault

__all__ = [
    "COOKIES_FIELD",
    "RESERVED_SETTINGS",
    "Actor",
    "LoadedSecrets",
    "SecretState",
    "SettingField",
    "SettingValueError",
    "SettingsSchema",
    "SettingsSchemaError",
    "SettingsView",
    "SourceSettings",
    "parse_settings_schema",
    "secret_row_id",
]

# Reserved words: a setting whose name holds one is refused until a native source needs it.
RESERVED_SETTINGS: Final = frozenset(
    {"proxy", "solver", "captcha", "impersonate", "tls_fingerprint", "browser", "headless"}
)
COOKIES_FIELD: Final = "cookies"  # the jar; never a property name
_JAR: Final = "jar"
_TABLE: Final = "source_secret"
_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
_TYPES: Final = {"string", "integer", "number", "boolean"}
_PROPERTY_KEYS: Final = frozenset(
    {"type", "title", "description", "enum", "minimum", "maximum", "minLength", "maxLength",
     "default", "writeOnly", "format"}
)  # fmt: skip
_SCHEMA_KEYS: Final = frozenset(
    {"type", "properties", "required", "title", "description", "additionalProperties"}
)
_FORMATS: Final = frozenset({"password", "uri"})
_MAX_SECRET_CHARS: Final = 4096
_MAX_COOKIES: Final = 64
_COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{0,4096}")
_FINGERPRINT_INFO: Final = b"shelfhold/source-setting-fingerprint"


class Actor(Protocol):
    """Who is behind a change, for the audit (`headless.actor.Actor` has this shape)."""

    @property
    def actor_id(self) -> str | None: ...
    @property
    def session_id(self) -> str | None: ...
    @property
    def auth_time(self) -> datetime | None: ...


class SettingsSchemaError(ValueError):
    """The plugin's `settings_schema` is outside what the host accepts; the plugin is refused."""

    code = "source.settings_schema_invalid"


class SettingValueError(ValueError):
    """A value refused for a field. The field and a closed reason are named, never the value."""

    code = "source.setting_invalid"

    def __init__(self, field_name: str, reason: str) -> None:
        super().__init__(f"{field_name}: {reason}")
        self.field = field_name
        self.reason = reason


@dataclass(frozen=True)
class SettingField:
    name: str
    type: str
    secret: bool = False
    enum: tuple[Any, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    default: Any = None
    title: str | None = None

    def check(self, value: Any) -> Any:
        """The value as stored, or `SettingValueError` naming the field."""
        if self.type == "boolean":
            ok = isinstance(value, bool)
        elif self.type == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif self.type == "number":
            ok = isinstance(value, int | float) and not isinstance(value, bool)
        else:
            ok = isinstance(value, str)
        if not ok:
            raise SettingValueError(self.name, f"expected {self.type}")
        if self.enum and value not in self.enum:
            raise SettingValueError(self.name, "not one of the allowed values")
        if isinstance(value, int | float) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise SettingValueError(self.name, "below the minimum")
            if self.maximum is not None and value > self.maximum:
                raise SettingValueError(self.name, "above the maximum")
        if isinstance(value, str):
            if self.min_length is not None and len(value) < self.min_length:
                raise SettingValueError(self.name, "too short")
            limit = self.max_length if self.max_length is not None else _MAX_SECRET_CHARS
            if len(value) > limit:
                raise SettingValueError(self.name, "too long")
            if "\x00" in value or (self.secret and ("\n" in value or "\r" in value)):
                raise SettingValueError(self.name, "control character")
        return value


@dataclass(frozen=True)
class SettingsSchema:
    fields: tuple[SettingField, ...] = ()

    def field(self, name: str) -> SettingField | None:
        return next((f for f in self.fields if f.name == name), None)

    def defaults(self) -> dict[str, Any]:
        return {f.name: f.default for f in self.fields if not f.secret and f.default is not None}

    def validate_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Plain values only; `None` resets a field to its default. A secret goes through
        `SourceSettings.set_secret`, never here (it would land in a plain column)."""
        checked: dict[str, Any] = {}
        for name, value in values.items():
            spec = self.field(name)
            if spec is None:
                raise SettingValueError(name, "unknown setting")
            if spec.secret:
                raise SettingValueError(name, "a secret is set on its own, never as a value")
            checked[name] = None if value is None else spec.check(value)
        return checked

    def secret_field(self, name: str) -> SettingField:
        spec = self.field(name)
        if spec is None or not spec.secret:
            raise SettingValueError(name, "not a secret setting of this source")
        return spec


def _number(value: Any, what: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsSchemaError(f"{what} must be a number")
    return float(value)


def _length(value: Any, what: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SettingsSchemaError(f"{what} must be a non-negative integer")
    return value


def _reserved(name: str) -> bool:
    words = set(name.split("_"))
    return bool(words & RESERVED_SETTINGS) or any(r in name for r in RESERVED_SETTINGS if "_" in r)


def _property(name: str, raw: Any) -> SettingField:
    if not isinstance(raw, Mapping):
        raise SettingsSchemaError(f"property {name!r} must be an object")
    extra = set(raw) - _PROPERTY_KEYS
    if extra:
        raise SettingsSchemaError(f"property {name!r} uses unsupported keywords {sorted(extra)}")
    kind = raw.get("type")
    if kind not in _TYPES:
        raise SettingsSchemaError(f"property {name!r} must be a string, number or boolean")
    fmt = raw.get("format")
    if fmt is not None and fmt not in _FORMATS:
        raise SettingsSchemaError(f"property {name!r} has an unsupported format")
    write_only = raw.get("writeOnly", False)
    if not isinstance(write_only, bool):
        raise SettingsSchemaError(f"property {name!r}: writeOnly must be a boolean")
    secret = write_only or fmt == "password"
    enum = raw.get("enum", ())
    if not isinstance(enum, list | tuple):
        raise SettingsSchemaError(f"property {name!r}: enum must be a list")
    if secret and (kind != "string" or "default" in raw or enum):
        raise SettingsSchemaError(f"secret property {name!r} must be a string without default")
    title = raw.get("title")
    spec = SettingField(
        name=name, type=str(kind), secret=secret, enum=tuple(enum),
        minimum=_number(raw.get("minimum"), f"{name}.minimum"),
        maximum=_number(raw.get("maximum"), f"{name}.maximum"),
        min_length=_length(raw.get("minLength"), f"{name}.minLength"),
        max_length=_length(raw.get("maxLength"), f"{name}.maxLength"),
        default=raw.get("default"), title=title if isinstance(title, str) else None,
    )  # fmt: skip
    try:
        for value in spec.enum:
            spec.check(value)
        if spec.default is not None:
            spec.check(spec.default)
    except SettingValueError as error:
        raise SettingsSchemaError(f"property {name!r}: {error.reason} in enum or default") from None
    return spec


def parse_settings_schema(raw: Mapping[str, Any] | None) -> SettingsSchema:
    """The schema the host accepts: `{}` (no setting) or an object of flat typed properties."""
    if not raw:
        return SettingsSchema()
    if not isinstance(raw, Mapping):
        raise SettingsSchemaError("settings_schema must be an object")
    extra = set(raw) - _SCHEMA_KEYS
    if extra:
        raise SettingsSchemaError(f"settings_schema uses unsupported keywords {sorted(extra)}")
    if raw.get("type") != "object":
        raise SettingsSchemaError("settings_schema must be of type object")
    if raw.get("additionalProperties", False) is not False:
        raise SettingsSchemaError("settings_schema accepts no additional properties")
    properties = raw.get("properties", {})
    if not isinstance(properties, Mapping):
        raise SettingsSchemaError("settings_schema.properties must be an object")
    fields = []
    for name, spec in properties.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise SettingsSchemaError("a setting name is lower-case letters, digits and _")
        if name == COOKIES_FIELD:
            raise SettingsSchemaError("'cookies' is the host's own jar, never a setting")
        if _reserved(name):
            raise SettingsSchemaError(
                f"setting {name!r} is reserved (proxy, solver, TLS fingerprint, headless browser)"
            )
        fields.append(_property(name, spec))
    required = raw.get("required", [])
    if not isinstance(required, list) or any(r not in properties for r in required):
        raise SettingsSchemaError("settings_schema.required names unknown properties")
    return SettingsSchema(tuple(fields))


def secret_row_id(plugin_id: str, kind: str, field_name: str) -> str:
    """The row id, which is also the row coordinate of the envelope's associated data."""
    return f"{plugin_id}/{kind}/{field_name}"


@dataclass(frozen=True)
class SecretState:
    set: bool
    fingerprint: str | None = None


@dataclass(frozen=True)
class SettingsView:
    """What the API and the CLI show: plain values, and of each secret only whether it is set."""

    values: dict[str, Any]
    secrets: dict[str, SecretState]
    cookies: SecretState


@dataclass(frozen=True)
class LoadedSecrets:
    """The opened secrets of one source, for the host's own injection only."""

    values: Mapping[str, str] = field(default_factory=dict)
    cookies: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"LoadedSecrets({len(self.values)} values, {len(self.cookies)} cookies)"

    __str__ = __repr__


def cookie_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in sorted(cookies.items()))


def _register_cookies(cookies: Mapping[str, str]) -> None:
    for name, value in cookies.items():
        register_secret_value(value)
        register_secret_value(f"{name}={value}")
    if cookies:
        register_secret_value(cookie_header(cookies))


def _check_cookies(cookies: Mapping[str, str]) -> dict[str, str]:
    if len(cookies) > _MAX_COOKIES:
        raise SettingValueError(COOKIES_FIELD, "too many cookies")
    checked = {}
    for name, value in cookies.items():
        if not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name):
            raise SettingValueError(COOKIES_FIELD, "a cookie name is not a token")
        if not isinstance(value, str) or not _COOKIE_VALUE.fullmatch(value):
            raise SettingValueError(COOKIES_FIELD, "a cookie value has a forbidden character")
        checked[name] = value
    return checked


def write_audit(connection: Connection, action: str, target: str, actor: Actor | None) -> None:
    connection.execute(
        sa.insert(identity.audit).values(
            id=new_ulid(), actor_id=actor.actor_id if actor else None, action=action,
            target=target, session_id=actor.session_id if actor else None,
            auth_time=actor.auth_time if actor else None,
        )
    )  # fmt: skip


class SourceSettings:
    """Reads and writes the settings of the sources; the only code that opens their secrets."""

    def __init__(
        self,
        engine: Engine,
        vault: SecretVault | Callable[[], SecretVault],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self._vault = vault
        self._now = now or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return "SourceSettings()"

    def vault(self) -> SecretVault:
        return self._vault if isinstance(self._vault, SecretVault) else self._vault()

    # -- reading -------------------------------------------------------------------------------

    def view(self, plugin_id: str, schema: SettingsSchema) -> SettingsView:
        with self.engine.connect() as connection:
            stored = self._plain(connection, plugin_id)
            rows = {
                (r.kind, r.field): r.fingerprint
                for r in connection.execute(
                    sa.select(
                        acquisition.source_secret.c.kind,
                        acquisition.source_secret.c.field,
                        acquisition.source_secret.c.fingerprint,
                    ).where(acquisition.source_secret.c.plugin_id == plugin_id)
                )
            }
        plain = {f.name for f in schema.fields if not f.secret}
        values = {**schema.defaults(), **{k: v for k, v in stored.items() if k in plain}}
        secrets = {
            f.name: SecretState(("secret", f.name) in rows, rows.get(("secret", f.name)))
            for f in schema.fields
            if f.secret
        }
        jar = rows.get(("cookies", _JAR))
        return SettingsView(values, secrets, SecretState(jar is not None, jar))

    def load(self, plugin_id: str) -> LoadedSecrets:
        """Open every secret and the cookie jar of a source; each value is registered for
        scrubbing, in all its forms, before this returns. A value that does not open raises."""
        vault = self.vault()
        values: dict[str, str] = {}
        cookies: dict[str, str] = {}
        table = acquisition.source_secret
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(table).where(table.c.plugin_id == plugin_id).order_by(table.c.id)
            ).mappings()
            for row in rows:
                plain = vault.open_columns(connection, _TABLE, row["id"], row)
                if row["kind"] == "cookies":
                    jar = json.loads(plain)
                    cookies = {str(k): str(v) for k, v in jar.items()}
                    _register_cookies(cookies)
                else:
                    text = plain.decode("utf-8")
                    register_secret_value(text)
                    values[row["field"]] = text
        return LoadedSecrets(values, cookies)

    # -- writing -------------------------------------------------------------------------------

    def set_values(
        self, plugin_id: str, schema: SettingsSchema, values: Mapping[str, Any], *, actor: Actor
    ) -> SettingsView:
        checked = schema.validate_values(values)
        with self.engine.begin() as connection:
            merged = {**self._plain(connection, plugin_id)}
            for name, value in checked.items():
                if value is None:
                    merged.pop(name, None)
                else:
                    merged[name] = value
            config = acquisition.source_config
            connection.execute(
                sqlite_insert(config)
                .values(plugin_id=plugin_id, settings=merged)
                .on_conflict_do_update(
                    index_elements=["plugin_id"],
                    set_={"settings": merged, "updated_at": self._now()},
                )
            )
            for name in sorted(checked):
                write_audit(connection, "source.settings_changed", f"{plugin_id}:{name}", actor)
        return self.view(plugin_id, schema)

    def set_secret(
        self, plugin_id: str, schema: SettingsSchema, name: str, value: str, *, actor: Actor
    ) -> SecretState:
        spec = schema.secret_field(name)
        if not value:
            raise SettingValueError(name, "empty")
        spec.check(value)
        register_secret_value(value)  # known before anything can log it
        state = self._seal(plugin_id, "secret", name, value.encode("utf-8"), value)
        with self.engine.begin() as connection:
            write_audit(connection, "source.secret_set", f"{plugin_id}:{name}", actor)
        return state

    def clear_secret(
        self, plugin_id: str, schema: SettingsSchema, name: str, *, actor: Actor
    ) -> SecretState:
        schema.secret_field(name)
        self._delete(
            plugin_id, "secret", name, "source.secret_cleared", f"{plugin_id}:{name}", actor
        )
        return SecretState(False)

    def set_cookies(
        self, plugin_id: str, cookies: Mapping[str, str], *, actor: Actor
    ) -> SecretState:
        checked = _check_cookies(cookies)
        target = f"{plugin_id}:{COOKIES_FIELD}"
        if not checked:
            self._delete(plugin_id, "cookies", _JAR, "source.cookies_cleared", target, actor)
            return SecretState(False)
        _register_cookies(checked)
        document = json.dumps(checked, sort_keys=True, separators=(",", ":"))
        state = self._seal(plugin_id, "cookies", _JAR, document.encode("utf-8"), document)
        with self.engine.begin() as connection:
            write_audit(connection, "source.cookies_set", target, actor)
        return state

    # -- rows ----------------------------------------------------------------------------------

    @staticmethod
    def _plain(connection: Connection, plugin_id: str) -> dict[str, Any]:
        stored = connection.execute(
            sa.select(acquisition.source_config.c.settings).where(
                acquisition.source_config.c.plugin_id == plugin_id
            )
        ).scalar()
        return dict(stored) if isinstance(stored, Mapping) else {}

    def _fingerprint(self, vault: SecretVault, row_id: str, text: str) -> str:
        subkey = hmac.new(vault.keyring.active.material, _FINGERPRINT_INFO, hashlib.sha256)
        digest = hmac.new(subkey.digest(), f"{row_id}\x00{text}".encode(), hashlib.sha256)
        return digest.hexdigest()[:8]

    def _seal(
        self, plugin_id: str, kind: str, field_name: str, plaintext: bytes, text: str
    ) -> SecretState:
        vault = self.vault()
        row_id = secret_row_id(plugin_id, kind, field_name)
        envelope = vault.seal(plaintext, table=_TABLE, column=registry.SEALED_COLUMN, row_id=row_id)
        fingerprint = self._fingerprint(vault, row_id, text)
        columns = {**envelope.to_columns(), "fingerprint": fingerprint, "updated_at": self._now()}
        with self.engine.begin() as connection:
            registry.register_active_key(connection, vault.keyring)
            connection.execute(
                sqlite_insert(acquisition.source_secret)
                .values(id=row_id, plugin_id=plugin_id, kind=kind, field=field_name, **columns)
                .on_conflict_do_update(index_elements=["id"], set_=columns)
            )
        return SecretState(True, fingerprint)

    def _delete(
        self, plugin_id: str, kind: str, field_name: str, action: str, target: str, actor: Actor
    ) -> None:
        row_id = secret_row_id(plugin_id, kind, field_name)
        with self.engine.begin() as connection:
            connection.execute(
                sa.delete(acquisition.source_secret).where(acquisition.source_secret.c.id == row_id)
            )
            write_audit(connection, action, target, actor)
