"""A monotone counter of identity changes, kept in `/data` and copied into the database.

Every revocation (session, API key, passkey) and every change of a password adds one. The file
`/data/.identity-counter` is written atomically (`write_private`) after the transaction that added
one and that stored the same number in `app_setting`. It is never part of a backup. A database put
back from a backup carries a number lower than the file's: that is how a start knows that
credentials revoked since are live again, whatever the sentinels say.

Limit: if the whole of `/data` is replaced, the file goes with it; the detection by the sentinels
of the libraries (`restored_behind`) is then the only one left.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import platform
from shelfhold.infrastructure.secrets.atomic import write_private

__all__ = ["COUNTER_FILE", "COUNTER_KEY", "IdentityCounter"]

COUNTER_FILE = ".identity-counter"
COUNTER_KEY = "identity.counter"
_MAX_BYTES = 32


class IdentityCounter:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / COUNTER_FILE

    def file_value(self) -> int | None:
        """None: no file. A file that cannot be read counts as the highest number (the safe side:
        it triggers the revocation, which rewrites the file)."""
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(_MAX_BYTES)
        except FileNotFoundError:
            return None
        except OSError:
            return 2**62
        try:
            return max(int(raw.strip() or b"0"), 0)
        except ValueError:
            return 2**62

    @staticmethod
    def db_value(connection: Connection) -> int:
        raw = connection.execute(
            sa.select(platform.app_setting.c.value).where(platform.app_setting.c.key == COUNTER_KEY)
        ).scalar()
        try:
            return int(raw) if raw is not None else 0
        except ValueError:
            return 0

    def bump(self, connection: Connection, *, at_least: int = 0) -> int:
        """Inside the caller's transaction: one more than the file and the database hold (and
        `at_least`), stored in the database. The file follows with `publish`, after the commit."""
        known = max(self.db_value(connection), self.file_value() or 0, at_least)
        return self.store(connection, known + 1)

    @staticmethod
    def store(connection: Connection, value: int) -> int:
        table = platform.app_setting
        updated = connection.execute(
            sa.update(table).where(table.c.key == COUNTER_KEY).values(value=str(value))
        ).rowcount
        if not updated:
            connection.execute(sa.insert(table).values(key=COUNTER_KEY, value=str(value)))
        return value

    def publish(self, value: int) -> None:
        """After the commit. A number lower than the file's never replaces it."""
        current = self.file_value()
        if current is None or value > current:
            write_private(self.path, str(value).encode("ascii"), overwrite=True)
