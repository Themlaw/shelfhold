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

    def paused_queues(self) -> list[str]:
        """Job kinds with paused work (shown to authenticated callers only, A7)."""
        database = self._settings.database_path
        if not database.exists():
            return []
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
            try:
                rows = connection.execute(
                    "SELECT DISTINCT kind FROM job_view WHERE status = 'paused' ORDER BY kind"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return []
        return [str(kind) for (kind,) in rows]

    def paused_reasons(self) -> dict[str, str]:
        """Paused job kind -> the code of the queue-wide pause that parked it (`disk.low`...);
        a job parked for another reason (a closed window) has no such code and is left out."""
        database = self._settings.database_path
        if not database.exists():
            return {}
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
            try:
                rows = connection.execute(
                    "SELECT kind, error_code FROM job_view WHERE status = 'paused' "
                    "AND error_code IS NOT NULL AND error_code != 'window.preempted' "
                    "ORDER BY kind"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return {}
        return {str(kind): str(code) for kind, code in rows}
