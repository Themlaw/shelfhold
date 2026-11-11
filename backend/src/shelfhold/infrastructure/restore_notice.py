"""The durable notice of a restore: its credentials were revoked, passkeys and passwords were not.

A restored database brings back every credential as it was on the day of the backup. Sessions and
API keys are revoked (they can be made again); a passkey or a password changed since the backup
is back in its old state, and only a person can tell. The notice is a small file in `/data`,
written where the restore lands (or by the first start on a database found behind its disk) and
removed by the reserved acknowledgement: until then `status` lists it, whatever restarts happen.
It holds dates and counts, never a secret.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["NOTICE_FILE", "RestoreNotice", "RestoreNoticeData"]

NOTICE_FILE = ".restore-notice.json"
_MAX_BYTES = 4096


@dataclass(frozen=True)
class RestoreNoticeData:
    restored_at: str  # ISO 8601, when the credentials were revoked
    backup_created_at: str | None  # the date of the backup, None when only the disk told
    sessions: int
    api_keys: int


class RestoreNotice:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / NOTICE_FILE

    def write(self, data: RestoreNoticeData, *, keep_existing: bool = False) -> bool:
        """Atomically, 0600. `keep_existing`: an earlier notice stays (it is older, and unseen).
        False when it stayed."""
        if keep_existing and self.read() is not None:
            return False
        body = json.dumps(data.__dict__, sort_keys=True).encode()
        temp = self.path.with_name(self.path.name + ".tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, body)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp, self.path)
        return True

    def note_revoked(self, revoked: tuple[int, int], *, now: datetime) -> None:
        """A start (or `reconcile`) found the database behind its disk and revoked the credentials
        itself: no backup date is known. A notice already there is kept."""
        self.write(RestoreNoticeData(now.isoformat(), None, *revoked), keep_existing=True)

    def read(self) -> RestoreNoticeData | None:
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read(_MAX_BYTES)
            document: Any = json.loads(raw)
            return RestoreNoticeData(
                restored_at=str(document["restored_at"]),
                backup_created_at=(
                    None if document.get("backup_created_at") is None
                    else str(document["backup_created_at"])
                ),
                sessions=int(document["sessions"]),
                api_keys=int(document["api_keys"]),
            )  # fmt: skip
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            # a notice that cannot be read is still a notice: the warning must not vanish with it
            return RestoreNoticeData("unknown", None, 0, 0)

    def acknowledge(self) -> bool:
        """True when a notice was there."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        with contextlib.suppress(OSError):
            self.path.with_name(self.path.name + ".tmp").unlink()
        return True
