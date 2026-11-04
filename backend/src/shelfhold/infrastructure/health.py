"""Probes behind `/healthz` and `/readyz` (ENF-OPS-003)."""

import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import MigrationStateFile, current_revision, head_revision


def _writable_dir(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)


def _database_writable(settings: Settings) -> bool:
    database = settings.database_path
    if not database.exists():
        return _writable_dir(settings.data_dir)  # it can still be created
    if not os.access(database, os.W_OK):
        return False
    try:
        connection = sqlite3.connect(database, timeout=1, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")  # takes the write lock, writes nothing
            connection.execute("ROLLBACK")
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return True


class HealthProbe:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def migration_progress(self) -> tuple[int, int] | None:
        return MigrationStateFile(self._settings.data_dir).read()

    def readiness(self) -> Mapping[str, bool]:
        settings = self._settings
        try:
            applied = current_revision(settings.database_path) == head_revision()
        except sqlite3.Error:
            applied = False
        return {
            "database_writable": _database_writable(settings),
            "migrations_applied": applied,
            "volumes_writable": _writable_dir(settings.data_dir)
            and _writable_dir(settings.store_dir),
        }
