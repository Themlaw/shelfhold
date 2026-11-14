"""Trust values a non-first-party plugin declares: proposed defaults until the admin attests them
(EF-EXP-065 (5), GAP-11-09, D15).

From M2 the rule applies to every plugin that is not first-party, sandboxed or not:
`content_trust: publisher` (its EPUBs skip the neutralizer), `access_partitioning: library|user`
(a shelf below 18+ may be bound to it) and `egress: foyer` (its files never leave the household)
are only proposals. The host applies the safe default (`untrusted`, `none`, `externe`) until the
admin attests the declared value for this instance; the attestation is audited, bound to the
installed version of the plugin, and lapses as soon as that version changes (new code nobody
vouched for).

The version is checked twice: `sync_installed` (at start-up and before an attestation) revokes,
with an audit line, the rows given for another version, and every read joins `plugin_install` and
compares the versions, so a row left behind never counts. A plugin the host does not know as
third-party is either first-party (nothing to attest) or unknown (refused).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import acquisition
from shelfhold.plugin_host.settings import Actor, write_audit

__all__ = [
    "ATTESTABLE",
    "FIRST_PARTY_PLUGIN_KEYS",
    "SAFE_DEFAULTS",
    "Attestation",
    "AttestationError",
    "AttestationNotNeeded",
    "AttestationStore",
    "InstalledVersion",
    "Retained",
    "publisher_attested",
    "retain",
]

# The values that need the admin's word, by field. Anything else is the safe default or invalid.
ATTESTABLE: Final[Mapping[str, frozenset[str]]] = {
    "content_trust": frozenset({"publisher"}),
    "access_partitioning": frozenset({"library", "user"}),
    "egress": frozenset({"foyer"}),
}
SAFE_DEFAULTS: Final[Mapping[str, str]] = {
    "content_trust": "untrusted",
    "access_partitioning": "none",
    "egress": "externe",
}
_ADAPTERS = Path(__file__).with_name("adapters")
# The host's own plugins: the adapters shipped under `plugin_host/adapters/{native,meta}/<key>/`.
FIRST_PARTY_PLUGIN_KEYS: Final = frozenset(
    package.name
    for group in ("native", "meta")
    for package in (_ADAPTERS / group).iterdir()
    if (package / "__init__.py").is_file()
)
THIRD_PARTY_ORIGIN: Final = "local"  # `plugin_install.origin` of a wheel the guard installed


class _Origin(StrEnum):  # the two values of `registry.PluginOrigin`, without importing it
    FIRST_PARTY = "first_party"
    THIRD_PARTY = "third_party"


class AttestationError(ValueError):
    code = "plugin.attestation_invalid"


class AttestationNotNeeded(AttestationError):
    """A first-party plugin's declared values apply as they are."""

    code = "plugin.attestation_not_needed"


@dataclass(frozen=True)
class InstalledVersion:
    key: str  # the plugin key (entry point name)
    dist: str  # the distribution that ships it
    version: str
    digest: str  # of the installed files, `sha256:<hex>`


@dataclass(frozen=True)
class Attestation:
    plugin_key: str
    field: str
    value: str
    plugin_version: str
    attested_at: datetime
    attested_by: str | None = None  # the user, when the account still exists
    attested_by_actor: str | None = None  # the session or the host CLI (also in the audit)
    plugin_sha256: str = ""  # the digest of the installed files the word was given for


@dataclass(frozen=True)
class Retained:
    values: dict[str, str]  # what the host applies
    proposed: dict[str, str] = field(default_factory=dict)  # declared, withheld for want of word


def retain(origin: object, declared: Mapping[str, str], attested: Mapping[str, str]) -> Retained:
    """The values the host applies for a plugin's declarations. A first-party plugin is taken at
    its word; any other keeps a declared trust value only if the admin attested that very value."""
    if str(getattr(origin, "value", origin)) == _Origin.FIRST_PARTY:
        return Retained(dict(declared))
    values: dict[str, str] = {}
    proposed: dict[str, str] = {}
    for name, value in declared.items():
        if name not in ATTESTABLE or value not in ATTESTABLE[name]:
            values[name] = value  # already the safe side: nothing to vouch for
        elif attested.get(name) == value:
            values[name] = value
        else:
            values[name] = SAFE_DEFAULTS[name]
            proposed[name] = value
    return Retained(values, proposed)


def _valid_rows(connection: Connection, plugin_key: str | None = None) -> list[sa.Row[Any]]:
    attestation, install = acquisition.plugin_attestation, acquisition.plugin_install
    query = (
        sa.select(attestation)
        .join(install, install.c.plugin_id == attestation.c.plugin_key)
        .where(
            install.c.origin == THIRD_PARTY_ORIGIN,
            attestation.c.plugin_version == install.c.version,
            attestation.c.plugin_sha256 == install.c.sha256,
        )
        .order_by(attestation.c.plugin_key, attestation.c.field)
    )
    if plugin_key is not None:
        query = query.where(attestation.c.plugin_key == plugin_key)
    return list(connection.execute(query))


def publisher_attested(connection: Connection, plugin_id: str | None) -> bool:
    """May a binding of this plugin say `publisher`? Yes for the host's own plugins; for a
    third-party one, only with an attestation of `publisher` for its installed version and files;
    never for a plugin the host does not know."""
    if plugin_id is None:
        return False
    install = acquisition.plugin_install
    third = connection.execute(
        sa.select(install.c.version).where(
            install.c.plugin_id == plugin_id, install.c.origin == THIRD_PARTY_ORIGIN
        )
    ).first()
    if third is None:
        return plugin_id in FIRST_PARTY_PLUGIN_KEYS
    return any(
        r.field == "content_trust" and r.value == "publisher"
        for r in _valid_rows(connection, plugin_id)
    )


def _as_attestation(row: sa.Row[Any]) -> Attestation:
    return Attestation(
        row.plugin_key,
        row.field,
        row.value,
        row.plugin_version,
        row.attested_at,
        row.attested_by,
        plugin_sha256=row.plugin_sha256,
    )


def _target(a: Attestation) -> str:
    return f"{a.plugin_key}@{a.plugin_version}:{a.field}={a.value}"


class AttestationStore:
    def __init__(self, engine: Engine, *, clock: object | None = None) -> None:
        self._engine = engine
        now = getattr(clock, "now", None)
        self._now: Callable[[], datetime] = now if callable(now) else (lambda: datetime.now(UTC))

    def sync_installed(self, installed: Mapping[str, InstalledVersion]) -> list[Attestation]:
        """Record the third-party plugins the guard installed, then revoke (and audit) every
        attestation given for a version or files that are no longer the installed ones."""
        install, attestation = acquisition.plugin_install, acquisition.plugin_attestation
        revoked: list[Attestation] = []
        with write_transaction(self._engine) as connection:
            for plugin in installed.values():
                digest = (
                    plugin.digest
                    if plugin.digest.startswith("sha256:")
                    else (f"sha256:{plugin.digest}")
                )
                values = {"version": plugin.version, "sha256": digest}
                connection.execute(
                    sqlite_insert(install)
                    .values(
                        plugin_id=plugin.key, api="", origin=THIRD_PARTY_ORIGIN, enabled=False,
                        **values,
                    )
                    .on_conflict_do_update(index_elements=["plugin_id"], set_=values)
                )  # fmt: skip
            stale = connection.execute(
                sa.select(attestation)
                .join(install, install.c.plugin_id == attestation.c.plugin_key)
                .where(
                    install.c.origin == THIRD_PARTY_ORIGIN,
                    sa.or_(
                        attestation.c.plugin_version != install.c.version,
                        attestation.c.plugin_sha256 != install.c.sha256,
                    ),
                )
                .order_by(attestation.c.plugin_key, attestation.c.field)
            ).all()
            for row in stale:
                gone = _as_attestation(row)
                connection.execute(
                    sa.delete(attestation).where(
                        attestation.c.plugin_key == gone.plugin_key,
                        attestation.c.field == gone.field,
                    )
                )
                write_audit(connection, "plugin.attestation_revoked", _target(gone), None)
                revoked.append(gone)
        return revoked

    def attest(
        self,
        plugin_key: str,
        field_name: str,
        value: str,
        *,
        actor: Actor,
        user_id: str | None = None,
    ) -> Attestation:
        """Attest one declared value for the installed version of a third-party plugin."""
        if field_name not in ATTESTABLE or value not in ATTESTABLE[field_name]:
            raise AttestationError(f"{field_name}={value} is not a trust value that needs a word")
        install = acquisition.plugin_install
        with write_transaction(self._engine) as connection:
            installed = connection.execute(
                sa.select(install.c.version, install.c.sha256).where(
                    install.c.plugin_id == plugin_key, install.c.origin == THIRD_PARTY_ORIGIN
                )
            ).first()
            if installed is None:
                if plugin_key in FIRST_PARTY_PLUGIN_KEYS:
                    raise AttestationNotNeeded(f"{plugin_key} is first-party: nothing to attest")
                raise AttestationError(f"{plugin_key} is not an installed third-party plugin")
            given = Attestation(
                plugin_key, field_name, value, str(installed.version), self._now(), user_id,
                actor.actor_id, plugin_sha256=str(installed.sha256),
            )  # fmt: skip
            columns = {
                "value": value, "plugin_version": given.plugin_version,
                "plugin_sha256": given.plugin_sha256,
                "attested_by": user_id, "attested_at": given.attested_at,
            }  # fmt: skip
            connection.execute(
                sqlite_insert(acquisition.plugin_attestation)
                .values(plugin_key=plugin_key, field=field_name, **columns)
                .on_conflict_do_update(index_elements=["plugin_key", "field"], set_=columns)
            )
            write_audit(connection, "plugin.attested", _target(given), actor)
        return given

    def withdraw(self, plugin_key: str, field_name: str, *, actor: Actor) -> bool:
        attestation = acquisition.plugin_attestation
        with write_transaction(self._engine) as connection:
            row = connection.execute(
                sa.select(attestation).where(
                    attestation.c.plugin_key == plugin_key, attestation.c.field == field_name
                )
            ).first()
            if row is None:
                return False
            connection.execute(
                sa.delete(attestation).where(
                    attestation.c.plugin_key == plugin_key, attestation.c.field == field_name
                )
            )
            write_audit(
                connection, "plugin.attestation_withdrawn", _target(_as_attestation(row)), actor
            )
        return True

    def revoke(self, plugin_key: str) -> list[Attestation]:
        """Every attestation of a plugin, gone (and audited): it was removed."""
        attestation = acquisition.plugin_attestation
        with write_transaction(self._engine) as connection:
            rows = connection.execute(
                sa.select(attestation).where(attestation.c.plugin_key == plugin_key)
            ).all()
            gone = [_as_attestation(r) for r in rows]
            connection.execute(sa.delete(attestation).where(attestation.c.plugin_key == plugin_key))
            for item in gone:
                write_audit(connection, "plugin.attestation_revoked", _target(item), None)
        return gone

    def listed(self, plugin_key: str) -> list[Attestation]:
        with self._engine.connect() as connection:
            return [_as_attestation(r) for r in _valid_rows(connection, plugin_key)]

    def valid(self) -> dict[str, dict[str, str]]:
        """Every attestation that holds, by plugin and field: for `PluginRegistry`."""
        out: dict[str, dict[str, str]] = {}
        with self._engine.connect() as connection:
            for row in _valid_rows(connection):
                out.setdefault(row.plugin_key, {})[row.field] = row.value
        return out
