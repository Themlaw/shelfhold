"""The headless configuration facade: what `shelfhold config|target|source|shelf` and the routes
`/api/v1/config|targets|sources` call (EF-INT-024).

It reuses what exists: `ShelfService` for shelves and bindings, `SecretVault` for every credential
(the Kavita admin key through `store_admin_key`), the notification `ChannelStore`. Nothing here
asks for SQL, and no secret is written anywhere but sealed: not in a log, a plan or a reply.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import structlog
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from shelfhold.acquisition.application.failures import FailureLedger
from shelfhold.acquisition.application.instance import reconnect
from shelfhold.config import ResolvedSettings
from shelfhold.export.application.preflight import DestinationNotWritableError
from shelfhold.export.application.root_bootstrap import (
    RootPolicy,
    RootRefusedError,
    register_roots,
    target_roots,
)
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.application.target_state import load_health, save_health
from shelfhold.export.domain.bindings import BindingError, check_ceiling
from shelfhold.export.domain.kavita_key_expiry import KeyNoticeState
from shelfhold.export.domain.path_map import PathEntry, PathMap, PathMapError, PathMode, within
from shelfhold.export.domain.reconcile import (
    CEILING_MISSING,
    FLOOR_MISSING,
    RATIO_MISSING,
    Thresholds,
)
from shelfhold.export.targets.kavita import AdminKey, load_admin_key, store_admin_key
from shelfhold.headless import records
from shelfhold.headless.actor import Actor
from shelfhold.headless.desired import (
    DesiredConfig,
    DesiredShelf,
    DesiredSource,
    DesiredTarget,
    parse_desired,
)
from shelfhold.headless.errors import (
    AdminRiskNotAccepted,
    ConfigFileError,
    DestinationNotWritable,
    InvalidConfigValue,
    ReadOnlyConfigKey,
    ShelfConflict,
    SourceAuthModeUnsupported,
    SourceUnreachable,
    SourceUpdateFiltersBlocking,
    TargetConflict,
    TargetExchangeFailed,
    UnknownConfigKey,
    UnknownTarget,
)
from shelfhold.headless.keys import KEYS, PATH_MAP_PREFIX, effective_check_mode, warning_of
from shelfhold.headless.ports import (
    ChannelValidator,
    KavitaKeyExchange,
    ShelfLinkPort,
    SuwayomiProbe,
)
from shelfhold.headless.urls import validate_target_url
from shelfhold.i18n import t
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.integrations.apprise.channels import ChannelStore
from shelfhold.integrations.apprise.destination import ValidatedChannel

__all__ = ["ConfigValue", "HeadlessAdmin", "Plan", "PlanChange"]

KEY_NAME = "shelfhold"
KEY_DAYS = 90  # the Kavita key expires; `target.key_expiring` warns at 14 and 1 days
ADMIN_RISK_ACTION = "target.admin_risk_accepted"  # ENF-SEC-013
MIN_PASSWORD_LENGTH = 8  # the log scrubber ignores shorter secrets (`MIN_KNOWN_SECRET`)
ROLES_UNVERIFIED_ACTION = "source.roles_unverified"  # EF-SRC-031, D1: the roles cannot be read
SETTINGS_FIXED_ACTION = "source.suwayomi_settings_fixed"  # the one write in Suwayomi's settings
ROLES_CONFIRMED_ACTION = "source.roles_confirmed"  # the admin vouches for the account
SUPPORTED_AUTH_MODES = frozenset({"none", "basic_auth", "ui_login"})
X_CEILING = 99
Action = Literal["create", "update", "unchanged"]
_log = structlog.get_logger()


@dataclass(frozen=True)
class ConfigValue:
    key: str
    value: object
    source: str  # default | db | env | toml | fixed
    default: object = None
    overridden_by: str | None = None
    warning: str | None = None  # a value that is accepted but does not act yet (EF-SRC-025)


ConfigValues = list[ConfigValue]
PathMapSpec = list[dict[str, str]]


@dataclass(frozen=True)
class TargetAdded:
    id: str
    name: str
    created: bool
    key_expires_at: datetime | None = None


@dataclass(frozen=True)
class SourceAdded:
    plugin_id: str
    auth_mode: str
    created: bool


@dataclass(frozen=True)
class ShelfCreated:
    shelf_id: str
    destination_id: str
    created: bool


@dataclass(frozen=True)
class PlanChange:
    kind: str  # config | notify | target | source | shelf
    subject: str
    action: Action
    before: object = None
    after: object = None
    secret: str | None = None  # "changed": a secret differs; never the value


@dataclass(frozen=True)
class Plan:
    changes: tuple[PlanChange, ...] = field(default_factory=tuple)

    @property
    def is_noop(self) -> bool:
        return all(change.action == "unchanged" for change in self.changes)

    def render(self) -> str:
        """The differences, one line each; never a secret (a URL of a channel is not shown)."""
        marks = {"create": "+", "update": "~", "unchanged": "="}
        lines = []
        for c in self.changes:
            line = f"{marks[c.action]} {c.kind} {c.subject}"
            if c.action == "update":
                line += f": {c.before!r} -> {c.after!r}"
            elif c.action == "create" and c.after is not None:
                line += f": {c.after!r}"
            if c.secret is not None:
                line += f" [secret: {c.secret}]"
            lines.append(line)
        return "\n".join(lines)


SecretReader = Callable[[Path], str | None]  # the first line of a secret file, None if unreadable


def read_local_secret(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return lines[0] if lines else ""


def _secret(read: SecretReader, path: Path | None, what: str) -> str | None:
    if path is None:
        return None
    line = read(path)
    if line is None:
        raise ConfigFileError(f"cannot read the {what} file")
    return line


def _http_url(url: str, what: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise InvalidConfigValue(f"{what} must be http(s)://host[:port] without credentials")
    return url.rstrip("/")


def _service_url(url: str, what: str) -> str:
    """The address of any service the administrator configures (ENF-SEC-001): the same rule for
    Kavita and Suwayomi, applied before the first request leaves."""
    clean = _http_url(url, what)
    try:
        return validate_target_url(clean)
    except ValueError as error:
        raise InvalidConfigValue(str(error)) from None


def _target_url(url: str) -> str:
    return _service_url(url, "the Kavita address")


def _suwayomi_address(url: str) -> str:
    return _service_url(url, "the Suwayomi address")


def _normalise_path_map(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise InvalidConfigValue("path_map is a list of {local, remote, mode}")
    out = []
    try:
        for entry in raw:
            if not isinstance(entry, dict):
                raise InvalidConfigValue("path_map is a list of {local, remote, mode}")
            built = PathEntry(
                str(entry["local"]), str(entry["remote"]), PathMode(str(entry.get("mode", "rw")))
            )
            out.append({"local": built.local, "remote": built.remote, "mode": built.mode.value})
    except (KeyError, ValueError, PathMapError):
        raise InvalidConfigValue("path_map: absolute paths, a mode of rw or adopt_ro") from None
    return out


def _overlap(a: str, b: str) -> bool:
    return within(a, b) is not None or within(b, a) is not None


def _ceiling(text: str) -> int:
    value = text.strip().upper().removesuffix("+")
    if value == "X":
        return X_CEILING
    if value.isdigit():
        return int(value)
    raise InvalidConfigValue("max age is a number of years such as 13+, or X")


WAKE_KINDS = ("follow.cycle", "export.run")


class HeadlessAdmin:
    def __init__(
        self,
        engine: Engine,
        vault: SecretVault,
        *,
        clock: Clock,
        kavita: KavitaKeyExchange,
        suwayomi: SuwayomiProbe,
        channels: ChannelValidator | None = None,
        resolve: Callable[[], ResolvedSettings] | None = None,
        roots: RootPolicy | None = None,
        linker: ShelfLinkPort | None = None,
    ) -> None:
        self._engine = engine
        self._vault = vault
        self._clock = clock
        self._kavita = kavita
        self._suwayomi = suwayomi
        self._channels = channels
        self._resolve = resolve
        self._roots = roots
        self._linker = linker
        self._shelves = ShelfService(engine)
        self._store = ChannelStore(vault, clock)

    def _wake(self) -> None:
        """A running server picks the follow and the export up at its next tick instead of at the
        next quarter hour: what was just configured is worth acting on now. No server: no row."""
        with self._engine.begin() as connection:
            records.wake_schedule(connection, WAKE_KINDS, self._clock.now())

    def _bootstrap(self) -> None:
        """The instance row and the roots the path maps name (idempotent; see `root_bootstrap`)."""
        if self._roots is None:
            return
        wanted = target_roots(self._engine)
        wanted.append((str(self._roots.store_dir), PathMode.RW))
        try:
            register_roots(self._engine, wanted, policy=self._roots)
        except RootRefusedError as refusal:
            raise InvalidConfigValue(str(refusal)) from None
        self._wake()

    def _default_path_map(self) -> list[dict[str, str]]:
        """EF-EXP-003: without a map, the shared folder is the parent of Kavita's media
        (`/library/manga <-> /manga`, books, comics); a folder that does not exist is skipped at
        registration, so only the ones the admin mounted become roots."""
        library = str(self._roots.library_dir) if self._roots is not None else "/library"
        named = self._named_locals()
        return _normalise_path_map(
            [{"local": e.local, "remote": e.remote, "mode": e.mode.value}
             for e in PathMap.default(library).entries
             if not any(_overlap(e.local, other) for other in named)]
        )  # fmt: skip

    def _named_locals(self, *, except_target: str | None = None) -> dict[str, PathMode]:
        """Every folder already named by a target's map or registered as a physical root."""
        named: dict[str, PathMode] = {}
        with self._engine.connect() as connection:
            for row in records.target_path_maps(connection):
                if row.id == except_target:
                    continue
                for entry in row.path_map or []:
                    named[entry["local"]] = PathMode(entry.get("mode", "rw"))
            for root in records.physical_roots(connection):
                named.setdefault(root.path_entry, PathMode(root.mode))
        return named

    def _check_roots(
        self, entries: list[dict[str, str]], *, except_target: str | None = None
    ) -> None:
        """Before a path map is stored: every folder it names must be allowed to be a root, and
        a read-only declaration cannot turn a folder already writable into a read-only one: its
        sentinel is written and the registered root stays `rw`."""
        if self._roots is None:
            return
        named = self._named_locals(except_target=except_target)
        for entry in entries:
            if entry.get("mode") != PathMode.ADOPT_RO.value:
                continue
            for other, mode in named.items():
                if mode is PathMode.RW and _overlap(entry["local"], other):
                    raise InvalidConfigValue(
                        f"{entry['local']} is already a writable root ({other}): it cannot be "
                        "declared adopt_ro; mount a dedicated read-only folder instead"
                    )
        try:
            self._roots.check_all([(e["local"], PathMode(e.get("mode", "rw"))) for e in entries])
        except RootRefusedError as refusal:
            raise InvalidConfigValue(str(refusal)) from None

    # -- config get | set | list --------------------------------------------------------------

    def get(self, key: str) -> ConfigValue:
        if key.startswith(PATH_MAP_PREFIX):
            return self._get_path_map(key)
        spec = KEYS.get(key)
        if spec is None:
            raise UnknownConfigKey(f"unknown key {key!r}")
        if spec.readonly:
            return ConfigValue(key, spec.default, "fixed", spec.default)
        if key == "notify.urls":
            with self._engine.connect() as connection:
                return ConfigValue(key, sorted(self._store.urls(connection)), "db", [])
        stored = self._setting(key)
        if stored is not None:
            overridden = self._overridden_by(key)
            return ConfigValue(key, stored, "db", spec.default, overridden, warning_of(key, stored))
        return ConfigValue(
            key, self._effective_default(key, spec.default), "default", spec.default,
            self._overridden_by(key),
        )  # fmt: skip

    def _effective_default(self, key: str, default: object) -> object:
        """`source.check_mode` is `piloted` by default when a WakeHook is configured (D8)."""
        if key == "source.check_mode" and self._resolve is not None:
            return effective_check_mode(
                None, wake_configured=self._resolve().values.wake_configured
            )
        return default

    def list(self) -> ConfigValues:
        rows = [self.get(name) for name in KEYS]
        with self._engine.connect() as connection:
            names = records.target_names(connection)
            rows += [self._get_path_map(PATH_MAP_PREFIX + name) for name in names]
        return rows

    def set(self, key: str, value: str) -> ConfigValue:
        if key.startswith(PATH_MAP_PREFIX):
            return self._set_path_map(key, value)
        spec = KEYS.get(key)
        if spec is None:
            raise UnknownConfigKey(f"unknown key {key!r}")
        if spec.readonly:
            raise ReadOnlyConfigKey(f"{key} is fixed in this release")
        if key == "notify.urls":
            self._set_channel(value)
            return self.get(key)
        validate = spec.validate
        if validate is None:  # a key with its own way of being set is handled above
            raise InvalidConfigValue(f"{key} cannot be set this way")
        clean = validate(value)
        if key.startswith("export.missing_"):
            self._check_thresholds(key, clean)
        with self._engine.begin() as connection:
            self._write_setting(connection, key, clean)
            if key == "instance.jurisdiction":
                records.set_jurisdiction(connection, clean, self._clock.now())
        _log.info("headless.config_set", key=key)
        self._bootstrap()
        return self.get(key)

    def _check_thresholds(self, key: str, clean: str) -> None:
        """The three numbers together: a floor above the ceiling would switch the guard off."""
        names = ("export.missing_floor", "export.missing_ratio", "export.missing_ceiling")
        stored = {n: v for n in names if (v := self._setting(n)) is not None}
        stored[key] = clean
        why = Thresholds(
            floor=int(stored.get(names[0], FLOOR_MISSING)),
            ratio=float(stored.get(names[1], RATIO_MISSING)),
            ceiling=int(stored.get(names[2], CEILING_MISSING)),
        ).problem()
        if why is not None:
            raise InvalidConfigValue(f"{key}: {why}")

    def _setting(self, key: str) -> str | None:
        with self._engine.connect() as connection:
            return records.setting(connection, key)

    @staticmethod
    def _write_setting(connection: Connection, key: str, value: str) -> None:
        records.write_setting(connection, key, value)

    def _overridden_by(self, key: str) -> str | None:
        """`env` or `toml` when a higher layer defines this key (ENF-OPS-004): the database value
        is then stored but not the effective one."""
        if self._resolve is None or key not in self._resolve().fields:
            return None
        state = self._resolve().fields[key]
        return state.source if state.source in {"env", "toml"} else None

    def _target_row(self, connection: Connection, name: str) -> Any:
        row = records.target_named(connection, name)
        if row is None:
            raise UnknownTarget(f"no target named {name!r}")
        return row

    def _get_path_map(self, key: str) -> ConfigValue:
        with self._engine.connect() as connection:
            row = self._target_row(connection, key.removeprefix(PATH_MAP_PREFIX))
        return ConfigValue(key, _normalise_path_map(list(row.path_map)), "db", [])

    def _set_path_map(self, key: str, value: str) -> ConfigValue:
        try:
            parsed = json.loads(value)
        except ValueError:
            raise InvalidConfigValue("path_map is JSON: a list of {local, remote, mode}") from None
        entries = _normalise_path_map(parsed)
        with self._engine.connect() as connection:
            own = self._target_row(connection, key.removeprefix(PATH_MAP_PREFIX)).id
        self._check_roots(entries, except_target=own)
        try:
            self._shelves.check_path_map(own, entries)  # bindings that would overlap or share
        except BindingError as error:
            raise InvalidConfigValue(f"{error.code}: {error}") from None
        with self._engine.begin() as connection:
            row = self._target_row(connection, key.removeprefix(PATH_MAP_PREFIX))
            before = list(row.path_map)
            records.set_path_map(connection, row.id, entries, self._clock.now())
        _log.info("headless.config_set", key=key)
        try:
            self._bootstrap()
        except Exception:  # the path map is stored only once the roots are registered
            with self._engine.begin() as connection:
                records.set_path_map(connection, row.id, before, self._clock.now())
            raise
        self._shelves.pause_family_of_target(own)  # audited again before anything is written
        return self.get(key)

    def _set_channel(self, value: str) -> None:
        name, separator, url = value.partition("=")
        if not separator or not name or not url:
            raise InvalidConfigValue("notify.urls takes name=url")
        if self._channels is None:
            raise InvalidConfigValue("notification channels are not available here")
        try:
            checked = self._channels.validate(url)
        except ValueError:
            raise InvalidConfigValue("this notification URL is refused") from None
        channel = ValidatedChannel(url=checked, scheme=urlsplit(checked).scheme,
                                   follows_redirects=False)  # fmt: skip
        try:
            with self._engine.begin() as connection:
                self._store.save(connection, name, channel)
        except ValueError:
            raise InvalidConfigValue("a channel name is a short lowercase slug") from None
        _log.info("headless.channel_saved", name=name)

    # -- target add kavita --------------------------------------------------------------------

    def target_add_kavita(
        self,
        *,
        name: str,
        url: str,
        username: str,
        password: str,
        accept_admin_risk: bool,
        path_map: PathMapSpec | None = None,
        actor: Actor | None = None,
    ) -> TargetAdded:
        """`actor` is the audit actor behind a server call; offline, the host user's uid goes in
        the target of the audit row (ENF-SEC-013: the accepted risk is journaled)."""
        clean_url = _target_url(url)
        entries = (
            _normalise_path_map(path_map) if path_map is not None else self._default_path_map()
        )
        self._check_roots(entries)  # before a key is exchanged or anything stored
        with self._engine.connect() as connection:
            existing = records.target_named(connection, name)
        if existing is not None:
            if existing.kind != "kavita" or existing.config.get("url") != clean_url:
                raise TargetConflict(f"a target named {name!r} exists with another address")
            if path_map is not None and _normalise_path_map(list(existing.path_map)) != entries:
                self._set_path_map(PATH_MAP_PREFIX + name, json.dumps(entries))
            self._bootstrap()
            return TargetAdded(existing.id, name, False)
        if not accept_admin_risk:  # before anything leaves: the warning is the whole answer
            raise AdminRiskNotAccepted
        exchanged = self._kavita.exchange(
            url=clean_url, username=username, password=password, key_name=KEY_NAME,
            expires_days=KEY_DAYS,
        )  # fmt: skip
        # From here the key exists in Kavita. If it cannot be kept, it is orphaned there: the
        # error names it so that the admin can revoke it (Kavita, user settings, auth keys).
        target_id = new_ulid()
        expires_at = exchanged.expires_at.isoformat() if exchanged.expires_at else None
        config = {
            "url": clean_url,
            "username": username,
            "key_name": KEY_NAME,
            "key_expires_at": expires_at,
        }
        try:
            with self._engine.begin() as connection:
                records.insert_kavita_target(
                    connection,
                    target_id=target_id,
                    name=name,
                    config=config,
                    path_map=entries,
                    version_seen=exchanged.version,
                )
                registry.register_active_key(connection, self._vault.keyring)
                store_admin_key(self._vault, connection, target_id, AdminKey(exchanged.value))
                self._audit_admin_risk(connection, name, actor)
        except SQLAlchemyError:
            ident = f" (id {exchanged.key_id})" if exchanged.key_id is not None else ""
            raise TargetExchangeFailed(
                f"the key {KEY_NAME!r}{ident} was created in Kavita but could not be saved:"
                " revoke it in Kavita, then run the command again"
            ) from None
        _log.info("headless.target_added", kind="kavita", name=name, url=clean_url)
        self._bootstrap()
        return TargetAdded(target_id, name, True, exchanged.expires_at)

    def _audit_admin_risk(self, connection: Connection, name: str, actor: Actor | None) -> None:
        """One row in the same transaction as the target: no accepted risk without its trace."""
        records.insert_audit(
            connection,
            audit_id=new_ulid(),
            actor_id=actor.actor_id if actor else None,
            action=ADMIN_RISK_ACTION,
            target=name if actor else f"{name} uid={os.getuid()}",
            at=self._clock.now(),
            session_id=actor.session_id if actor else None,
            auth_time=actor.auth_time if actor else None,
        )

    # -- the exits of EF-INT-023 that renew a credential ---------------------------------------

    def target_rotate_key(self, *, target: str, password: str) -> TargetAdded:
        """`target rotate-key`: a new named key from the dedicated admin account, sealed in place
        of the old one, which Kavita lets expire; the J-14 / J-1 notices start over."""
        with self._engine.connect() as connection:
            row = records.kavita_target(connection, target)
        if row is None:
            raise UnknownTarget(f"no Kavita target {target!r}")
        username = str(row.config.get("username", ""))
        if not username:
            raise InvalidConfigValue("this target has no recorded account: add it again")
        with self._engine.connect() as connection:
            old_key = load_admin_key(self._vault, connection, row.id).reveal()
        old_name = str(row.config.get("key_name", KEY_NAME))
        stamp = self._clock.now().strftime("%Y%m%d")
        new_name = f"{KEY_NAME}-{stamp}"
        exchanged = self._kavita.exchange(
            url=str(row.config["url"]), username=username, password=password,
            key_name=new_name, expires_days=KEY_DAYS,
        )  # fmt: skip
        expires_at = exchanged.expires_at.isoformat() if exchanged.expires_at else None
        with self._engine.begin() as connection:
            registry.register_active_key(connection, self._vault.keyring)
            store_admin_key(self._vault, connection, row.id, AdminKey(exchanged.value))
            records.set_target_config(
                connection,
                row.id,
                {**row.config, "key_expires_at": expires_at, "key_name": new_name},
            )
            health = load_health(connection, row.id)
            health.key_notices = KeyNoticeState(exchanged.expires_at, frozenset())
            health.key_expires_at = exchanged.expires_at
            health.refresh_waiting()
            save_health(connection, row.id, health)
        try:  # the new key is sealed: a failed revocation leaves the old one to expire
            revoked = self._kavita.revoke(
                url=str(row.config["url"]), username=username, password=password,
                key=old_key, key_name="" if old_name == new_name else old_name,
            )  # fmt: skip
        except TargetExchangeFailed:
            revoked = False
        _log.info("headless.target_key_rotated", name=row.name, old_key_revoked=revoked)
        return TargetAdded(row.id, row.name, False, exchanged.expires_at)

    def _suwayomi_url(self) -> str:
        with self._engine.connect() as connection:
            row = records.source_row(connection)
        settings = row.settings if row is not None else None
        if not isinstance(settings, dict) or not settings.get("base_url"):
            raise InvalidConfigValue("no Suwayomi source yet: add one with `shelfhold source add`")
        return str(settings["base_url"])

    def _resume_follow(self) -> None:
        with self._engine.begin() as connection:
            FailureLedger("suwayomi").resume(connection)

    def source_set_secret(self, secret: str) -> SourceAdded:
        """`source set-secret`: the new `user:password` is probed (mode, then sealed) and the
        follow cycle, paused on the expired secret, may start again."""
        added = self.source_add_suwayomi(url=self._suwayomi_url(), secret=secret)
        self._resume_follow()
        return added

    def source_recheck(self) -> SourceAdded:
        """`source recheck`: probe the server again with the stored secret (the mode may have
        been put back to one this release can use); the follow cycle resumes when it passes."""
        url = self._suwayomi_url()
        with self._engine.connect() as connection:
            try:
                raw = self._vault.open_row(connection, "source_config", "suwayomi")
            except Exception:  # no secret sealed: a source without authentication
                raw = b""
        document = json.loads(raw) if raw else {}
        secret = (
            f"{document['username']}:{document['password']}"
            if document.get("username") and document.get("password")
            else None
        )
        added = self.source_add_suwayomi(url=url, secret=secret)
        self._resume_follow()
        return added

    def source_reconnect(self) -> bool:
        """`source reconnect`: Suwayomi was installed again and the admin says it is the one to
        follow. The old fingerprint is forgotten and the follow resumes; the next cycle takes the
        new installation as its reference and finds the series again by their upstream identity.
        False when the service is not paused for that (nothing changes)."""
        with self._engine.begin() as connection:
            return reconnect(connection, "suwayomi", self._clock.now())

    def _audit_source(self, connection: Connection, action: str) -> None:
        """One row in the transaction of the change: no unverified account without its trace."""
        records.insert_audit(
            connection,
            audit_id=new_ulid(),
            actor_id=None,
            action=action,
            target=f"suwayomi uid={os.getuid()}",
            at=self._clock.now(),
            session_id=None,
            auth_time=None,
        )

    def source_confirm_roles(self) -> bool:
        """`source confirm-roles`: the admin vouches that the service account of a `ui_login`
        Suwayomi has minimal rights (the server exposes no role to check). Clears the
        `roles_unverified` warning of `status`; audited. False when nothing was to confirm."""
        with self._engine.begin() as connection:
            row = records.source_row(connection)
            if row is None or not row.settings.get("roles_unverified"):
                return False
            settings = {k: v for k, v in row.settings.items() if k != "roles_unverified"}
            records.update_source(connection, settings, self._clock.now())
            self._audit_source(connection, ROLES_CONFIRMED_ACTION)
        return True

    # -- source add suwayomi ------------------------------------------------------------------

    def source_add_suwayomi(
        self, *, url: str, secret: str | None, fix_suwayomi_settings: bool = False
    ) -> SourceAdded:
        clean_url = _suwayomi_address(url)
        username: str | None = None
        password: str | None = None
        if secret:
            username, separator, password = secret.partition(":")
            if not separator or not username or not password:
                raise InvalidConfigValue("the secret is user:password")
            if len(password) < MIN_PASSWORD_LENGTH:  # shorter values cannot be masked in the logs
                raise InvalidConfigValue(
                    f"the Suwayomi password must have at least {MIN_PASSWORD_LENGTH} characters: "
                    "the log scrubber cannot mask a shorter secret"
                )
        try:
            mode = self._suwayomi.auth_mode(url=clean_url, username=username, password=password)
        except ConnectionError:
            raise SourceUnreachable("Suwayomi cannot be reached at this address") from None
        except PermissionError:  # `ui_login`: the server refuses the account (nothing is stored)
            raise InvalidConfigValue("Suwayomi refuses this user:password account") from None
        except ValueError as too_powerful:  # `ui_login`: an account that may install extensions
            raise SourceAuthModeUnsupported(str(too_powerful)) from None
        if mode not in SUPPORTED_AUTH_MODES:
            raise SourceAuthModeUnsupported(t("headless.auth_mode_unsupported", mode=mode))
        if mode in ("basic_auth", "ui_login") and not (username and password):
            raise InvalidConfigValue("this server needs a user:password secret")
        fixed = False
        if self._suwayomi.blocking_filters and fix_suwayomi_settings:
            # Owner decision 2026-11-12: the admin confirmed; the only write in Suwayomi's settings
            try:
                self._suwayomi.blocking_filters = self._suwayomi.disable_update_filters(
                    url=clean_url, mode=mode, username=username, password=password
                )
            except ConnectionError:
                raise SourceUnreachable("Suwayomi cannot be reached at this address") from None
            except PermissionError:
                raise InvalidConfigValue("Suwayomi refuses this user:password account") from None
            except Exception:  # the server refused the write: the refusal below tells the way out
                _log.warning("headless.suwayomi_settings_fix_failed", url=clean_url)
            fixed = not self._suwayomi.blocking_filters
        if self._suwayomi.blocking_filters:  # EF-SRC-026 (d): blocks the wizard, with the fix
            raise SourceUpdateFiltersBlocking(
                t(
                    "headless.update_filters_blocking",
                    settings=", ".join(self._suwayomi.blocking_filters),
                )
            )
        keep = mode in ("basic_auth", "ui_login")
        settings: dict[str, Any] = {"base_url": clean_url, "auth_mode": mode}
        unverified = mode == "ui_login" and not self._suwayomi.roles_verified
        if unverified:  # a durable warning in `status` until the admin confirms (D1)
            settings["roles_unverified"] = True
        with self._engine.begin() as connection:
            if unverified:
                self._audit_source(connection, ROLES_UNVERIFIED_ACTION)
            if fixed:
                self._audit_source(connection, SETTINGS_FIXED_ACTION)
            created = records.source_row(connection) is None
            if created:
                records.insert_source(connection, settings)
            else:
                records.update_source(connection, settings, self._clock.now())
            if keep:
                document = json.dumps({"username": username, "password": password}).encode()
                self._vault.seal_row(connection, "source_config", "suwayomi", document)
        _log.info("headless.source_added", kind="suwayomi", url=clean_url, auth_mode=mode)
        self._bootstrap()
        return SourceAdded("suwayomi", mode, created)

    # -- shelf create -------------------------------------------------------------------------

    def shelf_create(
        self,
        *,
        name: str,
        max_age: str,
        target: str,
        subpath: str = "",
        phrase: str | None = None,
        actor: Actor | None = None,
    ) -> ShelfCreated:
        ceiling = _ceiling(max_age)
        check_ceiling(ceiling)  # a ceiling off the scale is refused before anything is stored
        with self._engine.connect() as connection:
            target_row = self._target_row(connection, target)
            shelf = records.shelf_named(connection, name)
            destination = None
            if shelf is not None:
                destination = records.shelf_binding(connection, shelf.id, target_row.id)
        if shelf is not None and shelf.max_age_rating != ceiling:
            raise ShelfConflict(f"the shelf {name!r} exists with another age ceiling")
        if shelf is not None and destination is not None:
            if destination.root_subpath != subpath:
                raise ShelfConflict(f"the shelf {name!r} is bound to another folder")
            return ShelfCreated(shelf.id, destination.id, False)
        shelf_id = (
            shelf.id
            if shelf is not None
            else self._shelves.create_shelf(
                name=name, phrase=phrase or name, max_age_rating=ceiling, actor=actor
            )
        )
        destination_id = self._shelves.bind_destination(
            shelf_id=shelf_id, target_id=target_row.id, transport="tree", plugin_egress="foyer",
            root_subpath=subpath,
        )  # fmt: skip
        try:
            self._bootstrap()  # the folder of the shelf lives under a registered root
            self._make_folder(destination_id)
            if self._linker is not None:
                self._linker.link(target_row.id, destination_id, library_name=name)
        except Exception:
            self._unbind(shelf_id, destination_id, shelf_was_new=shelf is None)
            raise
        _log.info("headless.shelf_created", name=name, target=target)
        return ShelfCreated(shelf_id, destination_id, True)

    def _make_folder(self, destination_id: str) -> None:
        """The folder of the destination exists before the first export and the library link: the
        permission probe and Kavita both look at it. It is made under a registered writable root,
        by the process that will write in it."""
        with self._engine.connect() as connection:
            local = load_context(connection, destination_id).root_local
        if local is None or self._roots is None or self._linker is None:
            return
        try:
            self._roots.check(local, PathMode.RW)
        except RootRefusedError as refusal:
            raise InvalidConfigValue(str(refusal)) from None
        try:
            self._linker.make_folder(local)
        except DestinationNotWritableError as refusal:  # EF-UX-002: the cause and the fix
            raise DestinationNotWritable(str(refusal)) from None

    def _unbind(self, shelf_id: str, destination_id: str, *, shelf_was_new: bool) -> None:
        """A shelf the audit refused leaves nothing behind (nothing was written in Kavita)."""
        with self._engine.begin() as connection:
            records.unbind(connection, shelf_id, destination_id, drop_shelf=shelf_was_new)

    # -- config apply -------------------------------------------------------------------------

    def apply_text(
        self,
        text: str,
        *,
        base_dir: Path,
        dry_run: bool = False,
        secrets: Mapping[str, str] | None = None,
        actor: Actor | None = None,
    ) -> Plan:
        """`apply` for a file the caller read: `secrets` maps the path of each secret file the
        text names to its first line (the caller is on the host, the server may not see it)."""
        read: SecretReader = (
            read_local_secret if secrets is None else lambda path: secrets.get(str(path))
        )
        return self.apply(
            parse_desired(text, base_dir=base_dir), dry_run=dry_run, read=read, actor=actor
        )

    def apply(
        self,
        desired: DesiredConfig,
        *,
        dry_run: bool = False,
        read: SecretReader = read_local_secret,
        actor: Actor | None = None,
    ) -> Plan:
        """The plan; with `dry_run` nothing is written, probed or exchanged."""
        plan = self._plan(desired, read)
        if dry_run:
            return plan
        for change in plan.changes:
            if change.action == "unchanged":
                continue
            self._execute(change, desired, read, actor)
        return plan

    def _plan(self, desired: DesiredConfig, read: SecretReader) -> Plan:
        changes: list[PlanChange] = []
        for key, value in desired.settings.items():
            spec = KEYS.get(key)
            if spec is None or spec.validate is None:
                raise UnknownConfigKey(f"unknown key {key!r}")
            clean = spec.validate(value)
            current = self._setting(key)
            changes.append(self._change("config", key, current, clean))
        if desired.notify:
            with self._engine.connect() as connection:
                have = self._store.urls(connection)
            for name, url in desired.notify.items():
                if self._channels is not None:
                    try:
                        self._channels.validate(url)
                    except ValueError:
                        raise InvalidConfigValue("a notification URL is refused") from None
                changes.append(
                    PlanChange(
                        "notify",
                        name,
                        "unchanged"
                        if have.get(name) == url
                        else ("update" if name in have else "create"),
                    )
                )
        for target in desired.targets:
            changes.append(self._plan_target(target, read))
        for source in desired.sources:
            changes.append(self._plan_source(source, read))
        for shelf in desired.shelves:
            changes.append(self._plan_shelf(shelf, desired))
        return Plan(tuple(changes))

    @staticmethod
    def _change(kind: str, subject: str, before: object, after: object) -> PlanChange:
        if before is None:
            return PlanChange(kind, subject, "create", None, after)
        action: Action = "unchanged" if before == after else "update"
        return PlanChange(kind, subject, action, before, after)

    def _plan_target(self, target: DesiredTarget, read: SecretReader) -> PlanChange:
        if target.kind != "kavita":
            raise ConfigFileError(f"target kind {target.kind!r}: only kavita in M1a")
        url = _target_url(target.url)
        wanted = _normalise_path_map(target.path_map) if target.path_map is not None else None
        with self._engine.connect() as connection:
            row = records.target_named(connection, target.name)
        if row is None:
            if not target.accept_admin_risk:
                raise AdminRiskNotAccepted
            if _secret(read, target.password_file, "password") is None:
                raise ConfigFileError("[[targets]]: password_file is required")
            return PlanChange("target", target.name, "create", None, url)
        if row.kind != "kavita" or row.config.get("url") != url:
            raise TargetConflict(f"a target named {target.name!r} exists with another address")
        if row.config.get("username") != target.user:  # the key belongs to that account
            raise TargetConflict(f"the target {target.name!r} was made with another Kavita account")
        if wanted is not None and _normalise_path_map(list(row.path_map)) != wanted:
            return PlanChange("target", target.name, "update",
                              _normalise_path_map(list(row.path_map)), wanted)  # fmt: skip
        return PlanChange("target", target.name, "unchanged")

    def _plan_source(self, source: DesiredSource, read: SecretReader) -> PlanChange:
        if source.kind != "suwayomi":
            raise ConfigFileError(f"source kind {source.kind!r}: only suwayomi in M1a")
        url = _suwayomi_address(source.url)
        with self._engine.connect() as connection:
            row = records.source_row(connection)
            if row is None:
                return PlanChange("source", "suwayomi", "create", None, url)
            changed = self._secret_changed(connection, row, source, read)
        current = row.settings.get("base_url")
        secret = "changed" if changed else None
        if current != url:
            return PlanChange("source", "suwayomi", "update", current, url, secret)
        if changed:
            return PlanChange("source", "suwayomi", "update", url, url, secret)
        return PlanChange("source", "suwayomi", "unchanged")

    def _secret_changed(
        self, connection: Connection, row: Any, source: DesiredSource, read: SecretReader
    ) -> bool:
        """Does the secret file say something else than what is sealed? Only the fact is kept."""
        if row.settings.get("auth_mode") not in ("basic_auth", "ui_login"):
            return False  # nothing is stored, and a server without login ignores a secret
        wanted = _secret(read, source.secret_file, "secret")
        if wanted is None:
            return False
        username, _, password = wanted.partition(":")
        if row.secret_kid is None:
            return True
        stored = json.loads(self._vault.open_row(connection, "source_config", "suwayomi"))
        return bool(stored != {"username": username, "password": password})

    def _plan_shelf(self, shelf: DesiredShelf, desired: DesiredConfig) -> PlanChange:
        ceiling = _ceiling(shelf.max_age)
        check_ceiling(ceiling)
        if shelf.target not in {t.name for t in desired.targets}:
            with self._engine.connect() as connection:
                self._target_row(connection, shelf.target)
        with self._engine.connect() as connection:
            row = records.shelf_named(connection, shelf.name)
            bound = None
            if row is not None:
                bound = records.bound_subpath(connection, row.id, shelf.target)
        if row is None or bound is None:
            return PlanChange("shelf", shelf.name, "create", None, shelf.target)
        if row.max_age_rating != ceiling or bound != shelf.subpath:
            raise ShelfConflict(f"the shelf {shelf.name!r} exists with other settings")
        return PlanChange("shelf", shelf.name, "unchanged")

    def _execute(
        self, change: PlanChange, desired: DesiredConfig, read: SecretReader, actor: Actor | None
    ) -> None:
        if change.kind == "config":
            self.set(change.subject, desired.settings[change.subject])
        elif change.kind == "notify":
            self._set_channel(f"{change.subject}={desired.notify[change.subject]}")
        elif change.kind == "target":
            target = next(t for t in desired.targets if t.name == change.subject)
            password = _secret(read, target.password_file, "password") or ""
            self.target_add_kavita(
                name=target.name, url=target.url, username=target.user, password=password,
                accept_admin_risk=target.accept_admin_risk, path_map=target.path_map,
                actor=actor,
            )  # fmt: skip
        elif change.kind == "source":
            source = next(s for s in desired.sources if s.kind == change.subject)
            self.source_add_suwayomi(
                url=source.url, secret=_secret(read, source.secret_file, "secret")
            )
        elif change.kind == "shelf":
            shelf = next(s for s in desired.shelves if s.name == change.subject)
            self.shelf_create(name=shelf.name, max_age=shelf.max_age, target=shelf.target,
                              subpath=shelf.subpath, phrase=shelf.phrase, actor=actor)  # fmt: skip
