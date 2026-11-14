"""`public_url` is read once, at start: what the running server uses is recorded in the database
at start-up, so that `shelfhold status` can say a later `config set public_url` waits for a restart.
"""

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import platform

__all__ = ["APPLIED_KEY", "applied_public_url", "record_applied_public_url"]

APPLIED_KEY = "runtime.public_url"


def record_applied_public_url(engine: Engine, public_url: str | None) -> None:
    table = platform.app_setting
    value = public_url or ""
    try:
        with write_transaction(engine) as connection:
            connection.execute(
                sqlite_insert(table)
                .values(key=APPLIED_KEY, value=value)
                .on_conflict_do_update(index_elements=["key"], set_={"value": value})
            )
    except OperationalError:  # no schema yet (before `migrate`): nothing to record
        return


def applied_public_url(engine: Engine) -> str | None:
    """What the server last started with; None when it never did (or recorded nothing)."""
    table = platform.app_setting
    with engine.connect() as connection:
        value = connection.execute(
            sa.select(table.c.value).where(table.c.key == APPLIED_KEY)
        ).scalar_one_or_none()
    return value
