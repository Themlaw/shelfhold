"""Database layer of the configuration (lowest precedence, ENF-OPS-004)."""

from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from shelfhold.infrastructure.db import create_db_engine


class SettingsStore:
    def __init__(self, database_path: Path) -> None:
        self._path = database_path

    def all(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        engine = create_db_engine(self._path)
        try:
            with engine.connect() as connection:
                rows = connection.execute(text("SELECT key, value FROM app_setting"))
                return {key: value for key, value in rows}
        except OperationalError as exc:
            if "no such table" in str(exc):  # migrations pending
                return {}
            raise
        finally:
            engine.dispose()


def load_db_settings(data_dir: Path) -> Mapping[str, str]:
    return SettingsStore(data_dir / "shelfhold.db").all()
