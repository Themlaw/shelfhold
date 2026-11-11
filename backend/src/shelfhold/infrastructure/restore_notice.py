"""The durable notice of a restore: its credentials were revoked, passkeys and passwords may not
be what they were.

A restored database brings back every credential as it was on the day of the backup. Sessions and
API keys are revoked (they can be made again). The notice is a row of `app_setting`, written in the
transaction of the revocation, so no crash can lose it; `status` lists it, and passkey logins are
refused, until the reserved `restore acknowledge` removes it. It holds dates, counts and names of
passkeys, never a secret.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import platform

__all__ = [
    "NOTICE_KEY",
    "RestoreNoticeData",
    "clear_notice",
    "notice_active",
    "read_notice",
    "write_notice",
]

NOTICE_KEY = "restore.notice"
_MAX_CHANGES = 50


@dataclass(frozen=True)
class RestoreNoticeData:
    restored_at: str  # ISO 8601, when the credentials were revoked
    backup_created_at: str | None  # the date of the backup, None when only the disk told
    sessions: int
    api_keys: int
    # The live database was readable: its revocations and password hashes were carried over.
    passwords_carried: bool = False
    passkey_changes: tuple[str, ...] = field(default=())  # "created: <name>" / "revoked: <name>"


def write_notice(connection: Connection, data: RestoreNoticeData, *, keep_existing: bool) -> None:
    """`keep_existing`: a notice already there stays (it is older, and nobody has seen it)."""
    table = platform.app_setting
    if keep_existing and read_notice(connection) is not None:
        return
    body = json.dumps(asdict(data) | {"passkey_changes": list(data.passkey_changes[:_MAX_CHANGES])})
    updated = connection.execute(
        sa.update(table).where(table.c.key == NOTICE_KEY).values(value=body)
    ).rowcount
    if not updated:
        connection.execute(sa.insert(table).values(key=NOTICE_KEY, value=body))


def read_notice(connection: Connection) -> RestoreNoticeData | None:
    table = platform.app_setting
    raw = connection.execute(sa.select(table.c.value).where(table.c.key == NOTICE_KEY)).scalar()
    if raw is None:
        return None
    try:
        document: Any = json.loads(raw)
        return RestoreNoticeData(
            restored_at=str(document["restored_at"]),
            backup_created_at=document.get("backup_created_at"),
            sessions=int(document["sessions"]),
            api_keys=int(document["api_keys"]),
            passwords_carried=bool(document.get("passwords_carried", False)),
            passkey_changes=tuple(str(c) for c in document.get("passkey_changes", ())),
        )
    except (ValueError, KeyError, TypeError):
        return RestoreNoticeData("unknown", None, 0, 0)  # a damaged notice still warns


def notice_active(connection: Connection) -> bool:
    table = platform.app_setting
    return (
        connection.execute(sa.select(table.c.key).where(table.c.key == NOTICE_KEY)).first()
        is not None
    )


def clear_notice(connection: Connection) -> bool:
    table = platform.app_setting
    return bool(connection.execute(sa.delete(table).where(table.c.key == NOTICE_KEY)).rowcount)
