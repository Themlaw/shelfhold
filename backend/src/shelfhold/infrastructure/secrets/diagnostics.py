"""Boot check of the keyring and the warnings `status` shows (ENF-SEC-005)."""

import base64
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import structlog

from shelfhold.config import Settings
from shelfhold.infrastructure.instance import LOCK_NAME  # the instance lock is the keyring lock
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.errors import KeyringMissingError
from shelfhold.infrastructure.secrets.keyring import Keyring, KeyringStore
from shelfhold.infrastructure.volumes import ensure_volumes

KEYRING_NAME = "keyring"


def keyring_path(settings: Settings) -> Path:
    return settings.secret_key_file or settings.data_dir / KEYRING_NAME


def store_for(settings: Settings) -> KeyringStore:
    return KeyringStore(
        keyring_path(settings),
        lock_path=settings.data_dir / LOCK_NAME,
        external=settings.secret_key_file is not None,
    )


def register_for_scrubbing(keyring: Keyring) -> None:
    for key in keyring.keys:
        register_secret_value(base64.b64encode(key.material).decode("ascii"))


def boot_check(settings: Settings, *, create: bool = True, announce: bool = True) -> Keyring:
    """Load the keyring or fail the start.

    An unreadable or corrupt keyring raises. A missing one is generated in `/data` only on a
    first start: if `SHELFHOLD_SECRET_KEY_FILE` names a file that is not there, or the database
    already holds keys or sealed secrets, a new keyring would orphan them, so the start fails.
    """
    store = store_for(settings)
    if not store.exists():
        if settings.secret_key_file is not None:
            raise KeyringMissingError(store.path, reason="configured_file_absent")
        try:
            holds_keys = registry.registry_has_keys(settings.database_path)
        except sqlite3.Error:
            # could not look: a fresh keyring over existing secrets would orphan them
            raise KeyringMissingError(store.path, reason="database_unreadable") from None
        if holds_keys:
            raise KeyringMissingError(store.path, reason="database_holds_keys")
        if not create:
            raise KeyringMissingError(store.path, reason="absent")
        ensure_volumes(settings)
        try:
            store.create(Keyring.generate())
            if announce:
                structlog.get_logger().warning("keyring_generated", path=str(store.path))
        except FileExistsError:
            pass  # another process created it first: use theirs
    keyring = store.load()
    register_for_scrubbing(keyring)
    return keyring


@dataclass(frozen=True)
class KeyringWarning:
    code: str
    detail: str = ""


def collect_warnings(settings: Settings) -> list[KeyringWarning]:
    """What `status` tells the admin about the keyring. Never reads key material."""
    store = store_for(settings)
    warnings: list[KeyringWarning] = []
    path = store.path
    try:
        inside = path.resolve().is_relative_to(settings.data_dir.resolve())
    except OSError:
        inside = False
    if inside:
        warnings.append(KeyringWarning("key_in_data", str(path)))
    if store.external and store.exists() and os.stat(path).st_mode & 0o004:
        warnings.append(KeyringWarning("keyring_world_readable", str(path)))
    if not store.exists():
        warnings.append(KeyringWarning("keyring_absent", str(path)))
        return warnings
    keyring = store.load()
    if keyring.rotation is not None:
        warnings.append(KeyringWarning("rotation_incomplete"))
    try:
        stale = registry.rows_not_on_key(settings.database_path, keyring.active.kid)
    except sqlite3.Error:
        stale = 0
    if stale:
        warnings.append(KeyringWarning("rows_on_retired_key", str(stale)))
    if keyring.exported_at is None:
        warnings.append(KeyringWarning("never_exported"))
    elif keyring.rotated_at is not None and keyring.exported_at < keyring.rotated_at:
        warnings.append(KeyringWarning("export_older_than_rotation"))
    warnings.extend(_tombstone_warnings(settings, keyring))
    return warnings


def _tombstone_warnings(settings: Settings, keyring: Keyring) -> list[KeyringWarning]:
    """Without the keyring the tombstones cannot be read (EF-BIB-054 (5), Q64): export it after
    the first one; and a registered kid missing from the keyring is the start-up check of
    "a cited kid is never retired" (ENF-SEC-005)."""
    try:
        kids = registry.tombstone_kids(settings.database_path)
    except sqlite3.Error:
        return []
    warnings: list[KeyringWarning] = []
    if kids.first_at is not None and (
        keyring.exported_at is None or keyring.exported_at < kids.first_at
    ):
        warnings.append(KeyringWarning("tombstones_not_exported"))
    missing = kids.kids - {key.kid for key in keyring.keys} - kids.lost
    if missing:
        warnings.append(KeyringWarning("tombstone_kid_missing", str(len(missing))))
    return warnings
