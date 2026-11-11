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

__all__ = ["COUNTER_FILE", "COUNTER_KEY", "DAMAGED", "IdentityCounter", "IdentityCounterError"]

COUNTER_FILE = ".identity-counter"
COUNTER_KEY = "identity.counter"
_MAX_BYTES = 32
DAMAGED = -1  # a file that is there but empty, unreadable or not a number


class IdentityCounterError(RuntimeError):
    """The counter's place holds something that is not a file: the start is refused, with a clear
    message, rather than revoking everything again at every start."""


class IdentityCounter:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / COUNTER_FILE

    def state(self) -> tuple[int | None, int | None]:
        """`(committed, pending)` of the file. `committed` is None without a file and `DAMAGED`
        for a file that is empty, unreadable or not understood (it counts as ahead of any database:
        the revocation runs once and the file is written again). A directory in its place raises
        `IdentityCounterError`. The old format, a bare number, reads as `committed`, no pending."""
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(_MAX_BYTES)
        except FileNotFoundError:
            return None, None
        except IsADirectoryError:
            raise IdentityCounterError(
                f"{self.path} is a directory: remove it so the identity counter can be a file"
            ) from None
        except OSError:
            return DAMAGED, None
        try:
            text = raw.decode("ascii").strip()
            if text.isdigit():  # the old format
                return int(text), None
            fields = dict(line.split("=", 1) for line in text.splitlines())
            if set(fields) - {"committed", "pending"} or "committed" not in fields:
                return DAMAGED, None
            pending = fields.get("pending")
            return int(fields["committed"]), None if pending is None else int(pending)
        except ValueError:  # empty included: an empty file is a torn write, not a zero
            return DAMAGED, None

    def file_value(self) -> int | None:
        """The committed number (see `state`)."""
        return self.state()[0]

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
        `at_least`), stored in the database. The file is written first, in two phases: now
        `committed=N, pending=N+1`, and `publish(N+1)` after the commit. A write that never
        commits leaves a pending the next start recognises and drops (no restore to suspect); a
        database put back from older state shows as `committed` above its own number."""
        known = max(self.db_value(connection), self.file_value() or 0, at_least)
        value = self.store(connection, known + 1)
        self._write(known, value)
        return value

    @staticmethod
    def store(connection: Connection, value: int) -> int:
        table = platform.app_setting
        updated = connection.execute(
            sa.update(table).where(table.c.key == COUNTER_KEY).values(value=str(value))
        ).rowcount
        if not updated:
            connection.execute(sa.insert(table).values(key=COUNTER_KEY, value=str(value)))
        return value

    def _write(self, committed: int, pending: int | None) -> None:
        text = f"committed={committed}\n" + (f"pending={pending}\n" if pending is not None else "")
        write_private(self.path, text.encode("ascii"), overwrite=True)

    def publish(self, value: int) -> None:
        """After the commit: `committed=value`, no pending. A number lower than the file's never
        replaces it; a damaged file is written again."""
        current = self.file_value()
        if current is None or current == DAMAGED or value >= current:
            self._write(value, None)

    def settle(self, database: int) -> None:
        """The file is not ahead of the database (`committed <= database`): an interrupted write
        is dropped, or adopted if the database did commit it. Never a restore."""
        committed, pending = self.state()
        if committed is None or committed == DAMAGED or committed > database:
            return
        if committed < database or pending is not None:
            self._write(database, None)
