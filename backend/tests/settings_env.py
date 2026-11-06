"""Test helper: write a row of `app_setting`, as the headless configuration does."""

from pathlib import Path

from sqlalchemy import text

from shelfhold.infrastructure.db import create_db_engine


def put_setting(database: Path, key: str, value: str) -> None:
    engine = create_db_engine(database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO app_setting (key, value) VALUES (:key, :value) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"key": key, "value": value},
            )
    finally:
        engine.dispose()
