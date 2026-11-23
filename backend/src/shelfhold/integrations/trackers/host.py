"""The host of the tracker plugins (M6 L13, EF-INT-010, D18, ENF-SEC-005).

One link per profile and per tracker plugin. The profile links the tracker of their own remote
account: the token is in the request once, sealed by the `SecretVault` to the `tracker_link` row,
and injected into the Fetcher the host hands the plugin (`AuthInjectingFetcher`, EF-SRC-023) —
the plugin never sees it: no argument, no log line, no answer carries it. A child profile is
refused unless the admin allowed this tracker to this profile (`tracker_allowance`, the reserved
action `tracker.allow_child`); withdrawing the allowance takes the child's link away.

The push leaves only after the profile's explicit accord (`push_enabled`, the reserved action
`tracker.push_opt_in`), and a pull is a `TrackerHint`: the host shows it and writes nothing —
no read mark, no position, no state (EF-INT-010).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from shelfhold_sdk.models import AuthApiKey, AuthBasic, PluginKind, SessionState
from shelfhold_sdk.tracker import TrackerHint, TrackerManifest, TrackerRef, TrackProgress
from sqlalchemy import Engine

from shelfhold.identity.application.ports import AuditEntry
from shelfhold.identity.public import audit_actor, audit_insert
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity, platform
from shelfhold.infrastructure.secrets import SecretVault, registry
from shelfhold.plugin_host.authflow import AuthInjectingFetcher
from shelfhold.plugin_host.registry import PluginRecord, PluginStatus

__all__ = ["LinkView", "TrackerError", "TrackerHost"]

TABLE: Final = "tracker_link"
_FINGERPRINT_INFO: Final = b"shelfhold/tracker-link-fingerprint"
_MIN_TOKEN: Final = 8
_MAX_TOKEN: Final = 512
_LOADED = frozenset({PluginStatus.LOADED, PluginStatus.LOADED_SHIM})

_link = platform.tracker_link
_allowance = platform.tracker_allowance
_user = identity.user

ERRORS: Final = frozenset(
    {"not_found", "child_refused", "refused", "push_refused", "not_linked", "invalid_token"}
)


class TrackerError(Exception):
    """A refusal; `code` is one of `ERRORS`. The message is the code, never a token."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LinkView:
    """What the API may show of a link: never the token, only its fingerprint."""

    user_id: str
    plugin_key: str
    linked_at: datetime
    push_enabled: bool
    token_fingerprint: str | None


def _check_token(token: object) -> str:
    if not isinstance(token, str) or not _MIN_TOKEN <= len(token) <= _MAX_TOKEN:
        raise TrackerError("invalid_token")
    if token != token.strip() or any(not c.isprintable() or c.isspace() for c in token):
        raise TrackerError("invalid_token")
    return token


def _origin_of(manifest: TrackerManifest) -> str | None:
    """The origin the credential is sent to: the `api` domain the plugin declared."""
    for rule in manifest.permissions.domains:
        if rule.purpose == "api":
            host = rule.pattern.removeprefix("*.").strip()
            if host:
                return f"https://{host}"
    return None


class TrackerHost:
    """The tracker plugins' host: resolves the plugin, injects the sealed token into the Fetcher,
    calls the contract, keeps the link. `registry_of()` gives the plugin registry (none loaded:
    every call answers `not_found`), `fetcher_for(origin)` the guarded fetcher of that origin."""

    def __init__(
        self,
        engine: Engine,
        vault_of: Callable[[], SecretVault],
        clock: Clock,
        *,
        registry_of: Callable[[], Any],
        fetcher_for: Callable[[str], Any],
    ) -> None:
        self._engine = engine
        self._vault_of = vault_of
        self._clock = clock
        self._registry_of = registry_of
        self._fetcher_for = fetcher_for

    def __repr__(self) -> str:
        return "TrackerHost()"

    # --- who may act ------------------------------------------------------------------------

    def _own(self, principal: Any, user_id: str) -> None:
        """The link is the profile's own: another profile's — the admin's too — answers like one
        that is not there (no probing)."""
        if user_id != principal.user_id:
            raise TrackerError("not_found")
        with self._engine.connect() as connection:
            if (
                connection.execute(sa.select(_user.c.id).where(_user.c.id == user_id)).first()
                is None
            ):
                raise TrackerError("not_found")

    def _child_allowed(self, user_id: str, plugin_key: str) -> bool:
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(_allowance.c.user_id).where(
                        _allowance.c.user_id == user_id,
                        _allowance.c.plugin_key == plugin_key,
                    )
                ).first()
                is not None
            )

    def _plugin(self, plugin_key: str) -> tuple[PluginRecord, TrackerManifest]:
        plugins = self._registry_of()
        record = plugins.get(plugin_key) if plugins is not None else None
        if record is None or record.status not in _LOADED or record.instance is None:
            raise TrackerError("not_found")
        manifest = getattr(record.instance, "manifest", None)
        if not isinstance(manifest, TrackerManifest) or manifest.kind != PluginKind.TRACKER:
            raise TrackerError("not_found")
        return record, manifest

    def _fetcher(self, manifest: TrackerManifest, token: str) -> Any:
        """The Fetcher the plugin is handed: the guarded one of the declared origin, wrapped so
        that it carries the account's credential. The token stays in the wrapper (EF-SRC-023)."""
        origin = _origin_of(manifest)
        flow = manifest.auth
        if origin is None or not isinstance(flow, AuthApiKey | AuthBasic):
            raise TrackerError("refused")
        secret = (
            {"api_key": token}
            if isinstance(flow, AuthApiKey)
            else {"username": "", "password": token}
        )
        return AuthInjectingFetcher(self._fetcher_for(origin), flow, secret, origin=origin)

    def _entry(self, action: str, principal: Any, target: str) -> AuditEntry:
        return AuditEntry(
            actor_id=audit_actor(principal),
            action=action,
            at=self._clock.now(),
            target=target,
            session_id=getattr(principal, "session_id", None),
            auth_time=getattr(principal, "auth_time", None),
        )

    def _fingerprint(self, row_id: str, token: str) -> str:
        subkey = hmac.new(
            self._vault_of().keyring.active.material, _FINGERPRINT_INFO, hashlib.sha256
        )
        digest = hmac.new(subkey.digest(), f"{row_id}\x00{token}".encode(), hashlib.sha256)
        return digest.hexdigest()[:8]

    def _row(self, user_id: str, plugin_key: str) -> Any:
        with self._engine.connect() as connection:
            return connection.execute(
                sa.select(_link).where(_link.c.user_id == user_id, _link.c.plugin_key == plugin_key)
            ).first()

    def _open_token(self, row: Any) -> str:
        with self._engine.connect() as connection:
            return self._vault_of().open_row(connection, TABLE, row.id).decode()

    # --- link, unlink -----------------------------------------------------------------------

    def link(self, principal: Any, user_id: str, plugin_key: str, token: object) -> LinkView:
        self._own(principal, user_id)
        if principal.role == "child" and not self._child_allowed(user_id, plugin_key):
            raise TrackerError("child_refused")
        record, manifest = self._plugin(plugin_key)
        secret = _check_token(token)
        fetcher = self._fetcher(manifest, secret)
        try:
            ref = asyncio.run(record.instance.link(fetcher, SessionState(), {}))
        except Exception:
            raise TrackerError("refused") from None
        if not isinstance(ref, TrackerRef):
            raise TrackerError("refused")
        now = self._clock.now()
        with write_transaction(self._engine) as connection:
            existing = connection.execute(
                sa.select(_link.c.id, _link.c.push_enabled).where(
                    _link.c.user_id == user_id, _link.c.plugin_key == plugin_key
                )
            ).first()
            row_id = existing.id if existing is not None else new_ulid()
            vault = self._vault_of()
            registry.register_active_key(connection, vault.keyring)
            envelope = vault.seal(
                secret.encode(), table=TABLE, column=registry.SEALED_COLUMN, row_id=row_id
            )
            fingerprint = self._fingerprint(row_id, secret)
            values: dict[str, Any] = {
                "remote_ref": ref.model_dump_json(),
                "linked_at": now,
                "token_fingerprint": fingerprint,
                **envelope.to_columns(),
            }
            if existing is None:
                connection.execute(
                    sa.insert(_link).values(
                        id=row_id, user_id=user_id, plugin_key=plugin_key, **values
                    )
                )
            else:
                connection.execute(sa.update(_link).where(_link.c.id == row_id).values(**values))
            action = "tracker.rotated" if existing is not None else "tracker.linked"
            connection.execute(
                audit_insert(self._entry(action, principal, f"{user_id}|{plugin_key}"))
            )
        return LinkView(
            user_id=user_id,
            plugin_key=plugin_key,
            linked_at=now,
            push_enabled=bool(existing.push_enabled) if existing is not None else False,
            token_fingerprint=fingerprint,
        )

    def unlink(self, principal: Any, user_id: str, plugin_key: str) -> bool:
        self._own(principal, user_id)
        with write_transaction(self._engine) as connection:
            gone = connection.execute(
                sa.delete(_link).where(_link.c.user_id == user_id, _link.c.plugin_key == plugin_key)
            )
            if gone.rowcount:
                connection.execute(
                    audit_insert(
                        self._entry("tracker.unlinked", principal, f"{user_id}|{plugin_key}")
                    )
                )
        return bool(gone.rowcount)

    def links(self, principal: Any, user_id: str) -> list[LinkView]:
        self._own(principal, user_id)
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(_link).where(_link.c.user_id == user_id).order_by(_link.c.created_at)
            )
            return [
                LinkView(
                    user_id=row.user_id,
                    plugin_key=row.plugin_key,
                    linked_at=row.linked_at,
                    push_enabled=bool(row.push_enabled),
                    token_fingerprint=row.token_fingerprint,
                )
                for row in rows
            ]

    def view(self, principal: Any, user_id: str, plugin_key: str) -> LinkView:
        """One link of the profile's own: the fingerprint is shown, the token never is."""
        self._own(principal, user_id)
        row = self._row(user_id, plugin_key)
        if row is None:
            raise TrackerError("not_found")
        return LinkView(
            user_id=row.user_id,
            plugin_key=row.plugin_key,
            linked_at=row.linked_at,
            push_enabled=bool(row.push_enabled),
            token_fingerprint=row.token_fingerprint,
        )

    # --- the push: only after the profile's explicit yes ------------------------------------

    def set_push(self, principal: Any, user_id: str, plugin_key: str, enabled: bool) -> bool:
        self._own(principal, user_id)
        with write_transaction(self._engine) as connection:
            changed = connection.execute(
                sa.update(_link)
                .where(_link.c.user_id == user_id, _link.c.plugin_key == plugin_key)
                .values(push_enabled=enabled)
            )
            if not changed.rowcount:
                raise TrackerError("not_linked")
            connection.execute(
                audit_insert(
                    self._entry(
                        "tracker.push_on" if enabled else "tracker.push_off",
                        principal,
                        f"{user_id}|{plugin_key}",
                    )
                )
            )
        return enabled

    def push(self, principal: Any, user_id: str, plugin_key: str, progress: TrackProgress) -> None:
        self._own(principal, user_id)
        row = self._row(user_id, plugin_key)
        if row is None:
            raise TrackerError("not_linked")
        if not row.push_enabled:
            raise TrackerError("push_refused")
        record, manifest = self._plugin(plugin_key)
        ref = TrackerRef.model_validate_json(row.remote_ref)
        fetcher = self._fetcher(manifest, self._open_token(row))
        try:
            asyncio.run(record.instance.push_progress(fetcher, ref, progress))
        except Exception:
            raise TrackerError("refused") from None

    # --- the pull: a hint, never a read mark -------------------------------------------------

    def pull(self, principal: Any, user_id: str, plugin_key: str) -> TrackerHint | None:
        self._own(principal, user_id)
        row = self._row(user_id, plugin_key)
        if row is None:
            raise TrackerError("not_linked")
        record, manifest = self._plugin(plugin_key)
        ref = TrackerRef.model_validate_json(row.remote_ref)
        fetcher = self._fetcher(manifest, self._open_token(row))
        try:
            hint = asyncio.run(record.instance.pull(fetcher, ref))
        except Exception:
            raise TrackerError("refused") from None
        return hint if hint is None or isinstance(hint, TrackerHint) else None

    # --- the admin's word for a child profile ------------------------------------------------

    def set_allowance(self, principal: Any, user_id: str, plugin_key: str, allowed: bool) -> bool:
        """The reserved action `tracker.allow_child`: a fresh admin session (the route's guard
        says it first). Allowing writes the admin's word; withdrawing it takes the child's link
        away with it — the link lived by this word."""
        if principal.role != "admin":
            raise TrackerError("not_found")
        now = self._clock.now()
        with write_transaction(self._engine) as connection:
            if (
                connection.execute(sa.select(_user.c.id).where(_user.c.id == user_id)).first()
                is None
            ):
                raise TrackerError("not_found")
            if allowed:
                there = connection.execute(
                    sa.select(_allowance.c.user_id).where(
                        _allowance.c.user_id == user_id,
                        _allowance.c.plugin_key == plugin_key,
                    )
                ).first()
                if there is None:
                    connection.execute(
                        sa.insert(_allowance).values(
                            user_id=user_id,
                            plugin_key=plugin_key,
                            allowed_by=principal.user_id,
                            allowed_at=now,
                        )
                    )
                else:  # the word is said again: who said it, and when, is the word now
                    connection.execute(
                        sa.update(_allowance)
                        .where(
                            _allowance.c.user_id == user_id,
                            _allowance.c.plugin_key == plugin_key,
                        )
                        .values(allowed_by=principal.user_id, allowed_at=now)
                    )
            else:
                connection.execute(
                    sa.delete(_allowance).where(
                        _allowance.c.user_id == user_id,
                        _allowance.c.plugin_key == plugin_key,
                    )
                )
                connection.execute(
                    sa.delete(_link).where(
                        _link.c.user_id == user_id, _link.c.plugin_key == plugin_key
                    )
                )
            connection.execute(
                audit_insert(
                    self._entry(
                        "tracker.allowance_withdrawn" if not allowed else "tracker.allowance_given",
                        principal,
                        f"{user_id}|{plugin_key}",
                    )
                )
            )
        return allowed
